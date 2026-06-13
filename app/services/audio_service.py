import os
import sys
import json
import asyncio
import subprocess
import logging
import time
import requests
from app.config import VIMEO_ACCESS_TOKEN, SERVER_BASE_URL, MUX_TOKEN_ID, MUX_TOKEN_SECRET
from app.services.mux_service import add_audio_track

logger = logging.getLogger(__name__)

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, "temp_audio")
# No yt-dlp disk cache — saves significant disk space on low-storage servers
CLEANUP_DELAY_SECONDS  = 15   # Mux fetches within a few seconds; 15s is more than enough
STALE_FILE_AGE_SECONDS = 600  # Purge any temp file older than 10 min on startup

# Limit concurrent audio downloads to protect disk space.
# Each download can be 50-200 MB; 3 concurrent = max ~600 MB in flight at once.
_DOWNLOAD_SEMAPHORE: asyncio.Semaphore | None = None

def _get_semaphore() -> asyncio.Semaphore:
    global _DOWNLOAD_SEMAPHORE
    if _DOWNLOAD_SEMAPHORE is None:
        _DOWNLOAD_SEMAPHORE = asyncio.Semaphore(3)
    return _DOWNLOAD_SEMAPHORE


MUX_BASE = "https://api.mux.com/video/v1"
MUX_AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)


# ── Startup cleanup ────────────────────────────────────────────────────────

def _purge_stale_temp_files():
    """Delete any leftover audio files from a previous crashed run."""
    if not os.path.isdir(TEMP_AUDIO_DIR):
        return
    now = time.time()
    purged = 0
    for fname in os.listdir(TEMP_AUDIO_DIR):
        fpath = os.path.join(TEMP_AUDIO_DIR, fname)
        if os.path.isfile(fpath):
            try:
                age = now - os.path.getmtime(fpath)
                if age > STALE_FILE_AGE_SECONDS:
                    os.remove(fpath)
                    purged += 1
            except Exception:
                pass
    if purged:
        logger.info(f"[Audio Service] Purged {purged} stale temp file(s) on startup.")

_purge_stale_temp_files()


# ── Mux helpers ────────────────────────────────────────────────────────────

def _mux_request(method: str, url: str, max_retries: int = 6, **kwargs) -> requests.Response:
    """Mux API call with 429 / 5xx retry + exponential backoff."""
    kwargs.setdefault("auth", MUX_AUTH)
    wait, srv_wait = 10, 5
    for attempt in range(1, max_retries + 1):
        r = requests.request(method, url, **kwargs)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", wait))
            logger.warning(f"[Mux] 429 on {method} {url} (attempt {attempt}/{max_retries}) — waiting {retry_after}s")
            if attempt == max_retries:
                return r
            time.sleep(retry_after)
            wait = min(wait * 2, 60)
        elif r.status_code >= 500:
            logger.warning(f"[Mux] {r.status_code} on {method} {url} (attempt {attempt}/{max_retries}) — waiting {srv_wait}s")
            if attempt == max_retries:
                return r
            time.sleep(srv_wait)
            srv_wait = min(srv_wait * 2, 60)
        else:
            return r
    return r


def _get_mux_audio_tracks(mux_asset_id: str) -> dict:
    """
    Returns existing audio tracks on a Mux asset.
    Uses GET /assets/{id} — the /tracks sub-endpoint returns 501.
    """
    r = _mux_request("GET", f"{MUX_BASE}/assets/{mux_asset_id}")
    if not r.ok:
        logger.warning(f"[Audio Service] Could not fetch Mux asset {mux_asset_id}: {r.status_code}")
        return {"names": set(), "languages": [], "total": 0}
    tracks = r.json().get("data", {}).get("tracks", [])
    audio  = [t for t in tracks if t.get("type") == "audio"]
    alt    = [t for t in audio  if not t.get("primary", False)]
    names  = {t.get("name", "") for t in audio}
    langs  = [t.get("language_code", "") for t in alt]
    return {"names": names, "languages": langs, "total": len(audio)}


# ── Vimeo / yt-dlp helpers ─────────────────────────────────────────────────

YT_DLP_BASE = [
    sys.executable, "-m", "yt_dlp",
    "--no-cache-dir",                                              # ← no disk cache
    "--add-header", f"Authorization: Bearer {VIMEO_ACCESS_TOKEN}",
    "--no-playlist",
]


def _to_player_url(vimeo_url: str) -> str:
    import re
    m = re.search(r'vimeo\.com/(\d+)', vimeo_url)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return vimeo_url


def _discover_audio_languages(vimeo_url: str) -> list[dict]:
    player_url = _to_player_url(vimeo_url)
    cmd = YT_DLP_BASE + ["--dump-json", player_url]
    logger.info(f"[Audio Service] Discovering audio languages via yt-dlp for {player_url}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"[Audio Service] yt-dlp discovery failed:\n{result.stderr[-300:]}")
            return []
        info    = json.loads(result.stdout)
        formats = info.get("formats", [])
        seen, tracks = set(), []
        for f in formats:
            lang = f.get("language")
            if f.get("vcodec") == "none" and f.get("acodec") != "none" and lang and lang not in seen:
                seen.add(lang)
                tracks.append({"language": lang, "name": f.get("format_note") or lang})
        logger.info(f"[Audio Service] Discovered {len(tracks)} audio language(s): {[t['language'] for t in tracks]}")
        return tracks
    except Exception as e:
        logger.error(f"[Audio Service] Error during audio discovery: {e}")
        return []


def _download_audio(vimeo_url: str, vimeo_id: str, language: str) -> str | None:
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    output_template = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.%(ext)s")
    player_url = _to_player_url(vimeo_url)
    cmd = YT_DLP_BASE + [
        "-f", f"bestaudio[ext=m4a][language={language}]/bestaudio[ext=m4a]/bestaudio[language={language}]/bestaudio",
        "-o", output_template,
        player_url,
    ]
    logger.info(f"[Audio Service] Downloading '{language}' audio from Vimeo...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.warning(f"[Audio Service] yt-dlp download failed ({language}):\n{result.stderr[-500:]}")
            return None
        if result.stdout:
            logger.info(f"[Audio Service] yt-dlp stdout ({language}):\n{result.stdout[-300:]}")
    except subprocess.TimeoutExpired:
        logger.error(f"[Audio Service] yt-dlp timed out for {vimeo_id} ({language})")
        return None

    for ext in ["mp4", "m4a", "mp3", "aac", "opus", "webm", "ogg"]:
        path = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.{ext}")
        if os.path.exists(path):
            logger.info(f"[Audio Service] ✅ Downloaded: {path}")
            return path

    try:
        files = os.listdir(TEMP_AUDIO_DIR)
        logger.warning(f"[Audio Service] Files matching {vimeo_id}: {[f for f in files if vimeo_id in f]}")
    except Exception:
        pass
    logger.warning(f"[Audio Service] Output file not found after yt-dlp for {vimeo_id} ({language})")
    return None


# ── Main background task ───────────────────────────────────────────────────

async def attach_audio_tracks_background(
    mux_asset_id: str,
    vimeo_id: str,
    vimeo_url: str,
    only_language: str | None = None,
) -> list[str]:
    """
    Background task triggered by video.asset.ready webhook.

    Flow:
      1. Fetch existing Mux tracks (to skip duplicates)
      2. Discover available audio languages from Vimeo via yt-dlp
      3. For each NEW language: download → serve locally → attach to Mux → delete temp file
      4. Sync DB with real Mux track count after all attachments
    """
    from app.database.session import SessionLocal
    from sqlalchemy import text

    logger.info(f"[Audio Service] Starting audio attachment for Vimeo {vimeo_id} → Mux {mux_asset_id}")

    # Step 1: Check what's already on Mux
    existing = await asyncio.to_thread(_get_mux_audio_tracks, mux_asset_id)
    existing_names = existing["names"]
    logger.info(f"[Audio Service] Existing Mux tracks: {existing_names or 'none'}")

    # Step 2: Discover Vimeo audio languages
    audio_tracks = await asyncio.to_thread(_discover_audio_languages, vimeo_url)
    if not audio_tracks:
        logger.info(f"[Audio Service] No alternate audio tracks found for {vimeo_id}.")
        return []

    if only_language:
        audio_tracks = [t for t in audio_tracks if t["language"] == only_language]
        if not audio_tracks:
            logger.warning(f"[Audio Service] Language '{only_language}' not found. Nothing to attach.")
            return []

    # Step 3: Download → attach → cleanup (one at a time, with semaphore)
    attached_languages = []
    skipped_languages  = []

    for track in audio_tracks:
        language = track["language"]
        name     = track["name"]

        # Skip if track with same name already exists on Mux
        if name in existing_names:
            logger.info(f"[Audio Service] ⏭️  '{name}' ({language}) already on Mux — skipping.")
            skipped_languages.append(language)
            continue

        file_path = None
        async with _get_semaphore():          # max 3 concurrent downloads across all videos
            try:
                file_path = await asyncio.to_thread(_download_audio, vimeo_url, vimeo_id, language)
                if not file_path:
                    logger.warning(f"[Audio Service] Skipping '{name}' ({language}) — download failed.")
                    continue

                filename   = os.path.basename(file_path)
                public_url = f"{SERVER_BASE_URL}/temp-audio/{filename}"
                logger.info(f"[Audio Service] Serving at: {public_url}")

                await asyncio.to_thread(add_audio_track, mux_asset_id, public_url, language, name)
                logger.info(f"[Audio Service] ✅ '{name}' ({language}) attached to Mux.")
                attached_languages.append(language)

                # Short wait so Mux can fetch the file before we delete it
                await asyncio.sleep(CLEANUP_DELAY_SECONDS)

            except Exception as e:
                logger.error(f"[Audio Service] ❌ Failed for '{name}' ({language}): {e}")
            finally:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"[Audio Service] Deleted temp file: {file_path}")

    # Step 4: Sync real Mux track count back to DB
    real = await asyncio.to_thread(_get_mux_audio_tracks, mux_asset_id)
    real_total    = real["total"]       # includes default track
    real_alt_langs = real["languages"]  # alternate tracks only

    if real_total > 0:
        try:
            with SessionLocal() as db:
                db.execute(
                    text("UPDATE videos SET audio_tracks_count=:c, audio_languages=:l WHERE mux_asset_id=:aid"),
                    {"c": real_total, "l": ", ".join(real_alt_langs), "aid": mux_asset_id}
                )
                db.commit()
            logger.info(
                f"[Audio] DB synced: {real_total} total track(s) "
                f"({len(attached_languages)} new, {len(skipped_languages)} already existed)"
            )
        except Exception as e:
            logger.error(f"[Audio Service] DB sync failed for {mux_asset_id}: {e}")

    return attached_languages
