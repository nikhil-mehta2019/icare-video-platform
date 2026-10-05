"""Downloads one Vimeo dubbed-audio rendition with yt-dlp and publishes it at a URL Mux can pull.

Mux's add-track API takes a URL, so the downloaded file must be publicly reachable until
Mux has fetched it. Default host = the existing platform server's POST /upload/temp-file
endpoint (served back from /temp-audio); `LocalTunnelHost` (--audio-host local) serves them
from this PC through an ngrok tunnel. Files are removed only after Mux reports the track ready/errored, and only
files this module published are ever deleted.
"""
import atexit
import glob
import http.server
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from urllib.parse import quote

import requests

from app.config import VIMEO_ACCESS_TOKEN
from migration.config import WORK_DIR, language_name, redact

logger = logging.getLogger(__name__)


class AudioFetchError(Exception):
    pass


def player_url(vimeo_id: str) -> str:
    return f"https://player.vimeo.com/video/{vimeo_id}"


def match_format_id(formats: list[dict], rendition_key: str) -> str | None:
    """yt-dlp format whose playlist URL carries this rendition's /av/<key>/ component
    (the format_id itself embeds a CDN name and a per-language index, so it is not stable)."""
    needle = f"/av/{rendition_key}/"
    for f in formats:
        if f.get("vcodec") == "none" and needle in (f.get("url") or ""):
            return f["format_id"]
    return None


RETRY_DELAYS = (5, 15, 30)   # waits before attempts 2, 3, 4 (max 4 attempts)

_PERMANENT = re.compile(r"HTTP Error (401|403|404)|\b(401|403|404) Client Error|Unsupported|Private video|"
                        r"Video unavailable|Unable to extract|Sign in|requested format|not a valid|Unauthorized|Forbidden",
                        re.I)
_TRANSIENT = [
    ("DNS resolution failure", re.compile(r"getaddrinfo|Failed to resolve|Name or service not known|"
                                          r"Temporary failure in name resolution", re.I)),
    ("connection reset", re.compile(r"Connection (reset|aborted)|ConnectionReset|Remote end closed|IncompleteRead|"
                                    r"Network is unreachable|SSL.*EOF", re.I)),
    ("connection timeout", re.compile(r"timed out|timeout", re.I)),
    ("temporary connection failure", re.compile(r"Connection (refused|error)|Temporary failure|Max retries exceeded", re.I)),
    ("HTTP 429", re.compile(r"HTTP Error 429|429 Client Error|Too Many Requests", re.I)),
    ("HTTP 5xx", re.compile(r"HTTP Error 50[0234]|50[0234] Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out", re.I)),
]


def classify_failure(text: str):
    """Reason string if the failure looks transient (worth retrying), else None (permanent or unrecognised)."""
    if _PERMANENT.search(text or ""):
        return None
    for reason, rx in _TRANSIENT:
        if rx.search(text or ""):
            return reason
    return None


class YtDlpAudioFetcher:
    def __init__(self, workdir=WORK_DIR, run=subprocess.run, max_info_age=900, clock=time.monotonic, sleep=time.sleep):
        self.workdir = workdir
        self._sleep = sleep
        self._run = run
        self._clock = clock
        self._max_age = max_info_age
        self._cache: dict = {}   # vimeo_id -> (timestamp, formats)

    def _cmd(self, *args):
        return [sys.executable, "-m", "yt_dlp", "--no-cache-dir", "--no-playlist", "--quiet", "--no-warnings",
                "--add-header", f"Authorization: Bearer {VIMEO_ACCESS_TOKEN}", *args]

    def _run_retrying(self, cmd, timeout, what, vimeo_id, label):
        """Runs yt-dlp up to 4 times; retries only transient failures and logs every retry."""
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                res = self._run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
                if res.returncode == 0:
                    return res
                text = res.stderr or ""
            except subprocess.TimeoutExpired:
                text = "yt-dlp timed out"
            reason = classify_failure(text)
            if not reason or attempt == attempts:
                kind = "permanent/unrecognised failure, not retried" if not reason else f"gave up after {attempts} attempts"
                raise AudioFetchError(f"yt-dlp {what} failed ({label}; {kind}): {redact(text[-300:])}")
            wait = RETRY_DELAYS[attempt - 1]
            logger.warning("Video: %s\nTrack: %s\nAttempt: %d/%d failed\nReason: %s\nRetrying in %d seconds",
                           vimeo_id, label, attempt, attempts, reason, wait)
            self._sleep(wait)

    def formats(self, vimeo_id: str, force: bool = False) -> list[dict]:
        hit = self._cache.get(vimeo_id)
        if hit and not force and self._clock() - hit[0] < self._max_age:
            return hit[1]
        res = self._run_retrying(self._cmd("--dump-json", player_url(vimeo_id)), 120, "format listing", vimeo_id,
                                 "format listing")
        fmts = json.loads(res.stdout).get("formats", [])
        self._cache[vimeo_id] = (self._clock(), fmts)
        return fmts

    def check_installed(self) -> str | None:
        try:
            import yt_dlp  # noqa: F401
            return yt_dlp.version.__version__
        except Exception:
            return None

    def download(self, vimeo_id: str, track) -> str:
        """Returns the local path of the downloaded audio file for this rendition."""
        os.makedirs(self.workdir, exist_ok=True)
        fid = match_format_id(self.formats(vimeo_id), track.rendition_key)
        if not fid:   # info may be stale or the CDN changed: re-read once before giving up
            fid = match_format_id(self.formats(vimeo_id, force=True), track.rendition_key)
        if not fid:
            raise AudioFetchError(f"no yt-dlp audio format matches rendition {track.rendition_key} ({track.language})")
        stem = f"f1_{vimeo_id}_{track.language}_{track.rendition_key}"
        self._run_retrying(self._cmd("-f", fid, "-o", os.path.join(self.workdir, stem + ".%(ext)s"), player_url(vimeo_id)),
                           900, "download", vimeo_id, language_name(track.language))
        for path in glob.glob(os.path.join(self.workdir, stem + ".*")):
            if not path.endswith((".part", ".ytdl")) and os.path.getsize(path) > 0:
                return path
        raise AudioFetchError(f"yt-dlp reported success but no output file found for {stem}")


class ServerUploadHost:
    """Publishes via the platform server's existing /upload/temp-file endpoint."""

    def __init__(self, base_url: str, session=requests):
        self.base = base_url.rstrip("/")
        self.session = session

    def check(self):
        try:
            r = self.session.get(self.base + "/", timeout=15)
            return r.status_code == 200, f"GET {self.base}/ -> {r.status_code}"
        except requests.RequestException as exc:
            return False, f"{self.base} unreachable ({type(exc).__name__})"

    def publish(self, path: str):
        """Uploads under a .m4a name (served as audio/mp4) and builds the public URL from OUR base URL.
        The server's own `url` field is ignored: it is built from the server's SERVER_BASE_URL, which may
        be a bare IP that its HTTP.sys front end rejects (HTTP 400 invalid hostname)."""
        stem, ext = os.path.splitext(os.path.basename(path))
        name = stem + ".m4a" if ext.lower() == ".mp4" else os.path.basename(path)
        with open(path, "rb") as fh:
            r = self.session.post(f"{self.base}/upload/temp-file/{quote(name)}", data=fh,
                                  headers={"Content-Type": "application/octet-stream"}, timeout=(30, 1800))
        if r.status_code >= 300:
            raise AudioFetchError(f"temp-file upload failed: HTTP {r.status_code} {r.text[:200]}")
        return f"{self.base}/temp-audio/{quote(name)}", name

    def release(self, name: str) -> None:
        try:
            self.session.delete(f"{self.base}/upload/temp-file/{quote(name)}", timeout=30)
        except requests.RequestException:
            logger.warning("could not delete temp file %s on server (it will be purged on server restart)", name)


class LocalTunnelHost:
    """Serves downloaded audio from THIS machine through an ngrok tunnel.

    Mux fetches tracks from the public internet, so localhost is useless: a minimal static file
    server (only the hosting dir, no directory listing, unguessable path prefix) is bound to
    127.0.0.1 and published by `ngrok http`. The production FastAPI app is deliberately NOT
    started (importing it runs create_all against the production database).
    """

    def __init__(self, serve_dir=None, ngrok_bin="ngrok", port=0):
        self.dir = serve_dir or os.path.join(os.path.dirname(WORK_DIR), "hosted_audio")
        self.ngrok_bin, self.port = ngrok_bin, port
        self.prefix = secrets.token_urlsafe(12)
        self.public_base = None
        self._httpd = self._proc = None

    def start(self):
        if self.public_base:
            return
        os.makedirs(self.dir, exist_ok=True)
        root, prefix = self.dir, "/" + self.prefix + "/"

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=root, **k)

            def translate_path(self, path):
                path = urllib.parse.unquote(path.split("?")[0])
                name = path[len(prefix):]
                if not path.startswith(prefix) or not name or "/" in name or "\\" in name or name.startswith("."):
                    return os.path.join(root, "__no_such_file__")   # 404: no listing, no traversal, flat dir only
                return os.path.join(root, name)

            def log_message(self, *a):
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        self._proc = subprocess.Popen([self.ngrok_bin, "http", f"127.0.0.1:{self.port}", "--log=stdout",
                                       "--log-format=json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, encoding="utf-8", errors="replace")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not self.public_base:
            line = self._proc.stdout.readline()
            if not line:
                break
            m = re.search(r"https://[a-z0-9.-]+\.ngrok[a-z0-9.-]*\.(?:app|io|dev)", line)
            if m:
                self.public_base = m.group(0)
        if not self.public_base:
            self.close()
            raise AudioFetchError("ngrok did not report a public URL (is another ngrok session running?)")
        threading.Thread(target=lambda: [None for _ in self._proc.stdout], daemon=True).start()   # keep pipe drained
        atexit.register(self.close)

    def close(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self.public_base = None

    def url_for(self, name):
        return f"{self.public_base}/{self.prefix}/{quote(name)}"

    def check(self):
        """Real round trip: write a tiny file, fetch it through the PUBLIC url, compare bytes."""
        try:
            self.start()
            name, body = "selftest.m4a", os.urandom(64)
            with open(os.path.join(self.dir, name), "wb") as fh:
                fh.write(body)
            try:
                for _ in range(6):   # the edge can answer 404 for a moment after ngrok reports the URL
                    r = requests.get(self.url_for(name), timeout=30)
                    if r.status_code == 200:
                        break
                    time.sleep(3)
            finally:
                self.release(name)
            ok = r.status_code == 200 and r.content == body
            return ok, f"public GET -> {r.status_code}, content-type {r.headers.get('content-type')}, bytes match: {r.content == body}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {redact(exc)}"

    def publish(self, path: str):
        self.start()
        name = os.path.basename(path)
        shutil.copy2(path, os.path.join(self.dir, name))
        return self.url_for(name), name

    def release(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.dir, name))
        except OSError:
            pass
