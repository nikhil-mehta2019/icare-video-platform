"""Read-only Vimeo discovery for the Folder 1 migration.

Reuses the existing token (app.config) and folder walker
(app.services.vimeo_service.get_vimeo_folder_videos). Everything here is GET-only.

Audio: Vimeo exposes the dubbed tracks as HLS audio renditions
(#EXT-X-MEDIA:TYPE=AUDIO). The rendition flagged DEFAULT=YES without
CHARACTERISTICS="public.translation.dubbed" is the original audio embedded in the
video file; every dubbed rendition (including the English one, which is a *different*
rendition from the embedded English) is a separate track. Each rendition is keyed by
the `/av/<id>/` component of its playlist URL, which is stable across CDNs.
"""
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

from app.config import VIMEO_ACCESS_TOKEN
from migration.config import EXPECTED_LANGUAGES, base_lang, language_name

logger = logging.getLogger(__name__)

API = "https://api.vimeo.com"
DUBBED_FLAG = "public.translation.dubbed"


class VimeoError(Exception):
    pass


# --------------------------------------------------------------------------- data


@dataclass
class VideoInfo:
    vimeo_id: str
    title: str
    url: str
    duration: int | None
    created_time: str | None
    modified_time: str | None
    status: str | None
    is_playable: bool
    transcode_status: str | None
    versions_total: int
    current_version_uri: str | None
    folder_name: str = "Root"


@dataclass
class AudioTrackInfo:
    language: str
    name: str            # name we will give the Mux track (English language name)
    vimeo_name: str      # NAME= from the manifest
    channels: str | None
    default: bool
    dubbed: bool
    playlist_url: str
    rendition_key: str


@dataclass
class AudioDiscovery:
    tracks: dict = field(default_factory=dict)      # base language -> AudioTrackInfo (dubbed only)
    original: list = field(default_factory=list)    # embedded/original audio renditions (not re-attached)
    duplicates: list = field(default_factory=list)  # dubbed renditions dropped because the language repeated
    error: str | None = None


@dataclass
class CaptionTrackInfo:
    language: str
    name: str
    vimeo_type: str | None
    vimeo_track_id: str
    vtt_url: str | None
    srt_url: str | None
    source_name: str | None
    active: bool
    autogen: bool
    source_format: str | None = None   # format of the URL we will give Mux
    source_url: str | None = None      # that URL


@dataclass
class VideoPlan:
    info: VideoInfo
    download: dict | None
    audio: AudioDiscovery
    captions: dict = field(default_factory=dict)    # base language -> CaptionTrackInfo
    caption_notes: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    problems: list = field(default_factory=list)    # {"stage","track_type","language","message"}

    @property
    def missing_audio(self):
        return [l for l in EXPECTED_LANGUAGES if l not in self.audio.tracks]

    @property
    def missing_captions(self):
        return [l for l in EXPECTED_LANGUAGES if l not in self.captions]


# ------------------------------------------------------------------------ HTTP


def _headers():
    return {"Authorization": f"Bearer {VIMEO_ACCESS_TOKEN}"}


class VimeoApi:
    """Thin GET-only client with retry/backoff; injectable so tests need no network."""

    def __init__(self, session=None, sleep=time.sleep):
        self.session = session or requests
        self.sleep = sleep

    def _get(self, url, params=None, retries=4, headers=None, **kw):
        last = None
        for attempt in range(1, retries + 1):
            try:
                r = self.session.get(url, headers=headers if headers is not None else _headers(),
                                     params=params, timeout=(5, 60), **kw)
            except requests.RequestException as exc:
                last = f"{type(exc).__name__}"
                self.sleep(2 ** attempt)
                continue
            if r.status_code in (200, 206):
                return r
            if r.status_code == 429 or r.status_code >= 500:
                wait = int(r.headers.get("Retry-After", 2 ** attempt)) if r.headers else 2 ** attempt
                last = f"HTTP {r.status_code}"
                self.sleep(min(wait, 60))
                continue
            raise VimeoError(f"GET {url.split('?')[0]} -> HTTP {r.status_code}: {r.text[:200]}")
        raise VimeoError(f"GET {url.split('?')[0]} failed after {retries} attempts ({last})")

    def get_json(self, path, params=None):
        url = path if path.startswith("http") else API + path
        return self._get(url, params=params).json()

    def get_video(self, vimeo_id):
        return self.get_json(f"/videos/{vimeo_id}")

    def get_versions(self, vimeo_id):
        return self.get_json(f"/videos/{vimeo_id}/versions", {"per_page": 100}).get("data", [])

    def get_texttracks(self, vimeo_id):
        out, url, params = [], f"/videos/{vimeo_id}/texttracks", {"per_page": 100}
        while url:
            data = self.get_json(url, params)
            out.extend(data.get("data", []))
            nxt = (data.get("paging") or {}).get("next")
            url, params = (API + nxt if nxt else None), None
        return out

    def get_manifest(self, link):
        """Returns (text, final_url) for an HLS master playlist (public CDN URL, no auth header)."""
        r = self._get(link, headers={}, allow_redirects=True)
        return r.content.decode("utf-8", errors="replace"), r.url

    def sniff_caption(self, url):
        """First ~1KB of a caption file -> 'srt' | 'vtt' | 'ttml' | None."""
        try:
            r = self._get(url, headers={"Range": "bytes=0-1023"}, retries=2)
        except VimeoError:
            return None
        return sniff_caption_format(r.content.decode("utf-8-sig", errors="replace"))

    def enumerate_folder(self, folder_id):
        from app.services.vimeo_service import get_vimeo_folder_videos
        return get_vimeo_folder_videos(folder_id)


# ------------------------------------------------------------ folder / videos


def video_id_from_uri(uri: str) -> str:
    return uri.rstrip("/").split("/")[-1]


def dedupe_videos(items):
    """items: [{"video": {...}, "folder_name": str}] -> (unique items, duplicate Vimeo IDs)."""
    seen, unique, dups = set(), [], []
    for it in items:
        vid = video_id_from_uri(it["video"]["uri"])
        if vid in seen:
            dups.append(vid)
            continue
        seen.add(vid)
        unique.append(it)
    return unique, dups


def parse_video(video: dict, folder_name: str = "Root") -> VideoInfo:
    vid = video_id_from_uri(video["uri"])
    versions = ((video.get("metadata") or {}).get("connections") or {}).get("versions") or {}
    return VideoInfo(
        vimeo_id=vid,
        title=video.get("name") or "Untitled",
        url=video.get("link") or f"https://vimeo.com/{vid}",
        duration=video.get("duration"),
        created_time=video.get("created_time"),
        modified_time=video.get("modified_time"),
        status=video.get("status"),
        is_playable=bool(video.get("is_playable", True)),
        transcode_status=(video.get("transcode") or {}).get("status"),
        versions_total=int(versions.get("total") or 0),
        current_version_uri=versions.get("current_uri"),
        folder_name=folder_name,
    )


def pick_best_download(video: dict) -> dict | None:
    """Highest-resolution progressive MP4 from files[] / download[] (HLS excluded).
    Ties prefer larger size, then files[] (the link style the existing migration already pulls)."""
    cands = []
    for src in ("files", "download"):
        for f in video.get(src) or []:
            if not f.get("link"):
                continue
            if f.get("rendition") == "adaptive" or f.get("quality") == "hls":
                continue
            ftype = f.get("type") or ""
            if ftype and not ftype.startswith("video/"):
                continue
            cands.append((f.get("height") or 0, f.get("width") or 0, f.get("size") or 0,
                          1 if src == "files" else 0, src, f))
    if not cands:
        return None
    best = max(cands, key=lambda c: c[:4])
    f = best[5]
    return {"link": f["link"], "height": f.get("height"), "width": f.get("width"),
            "size": f.get("size"), "rendition": f.get("rendition"),
            "quality": f.get("quality"), "source": best[4]}


def version_note(api, info: VideoInfo) -> str | None:
    """Explains which Vimeo version is current when a video has several."""
    if info.versions_total <= 1:
        return None
    versions = api.get_versions(info.vimeo_id)
    ordered = sorted(versions, key=lambda v: v.get("upload_date") or v.get("created_time") or "")
    cur_id = (info.current_version_uri or "").rstrip("/").split("/")[-1]
    labels = []
    cur_label = None
    for i, v in enumerate(ordered, 1):
        vid = video_id_from_uri(v["uri"])
        labels.append(f"V{i}={vid}")
        if vid == cur_id:
            cur_label = f"V{i}"
    if not cur_label:
        return (f"Vimeo reports {len(ordered)} versions but the current version ({cur_id or 'unknown'}) "
                f"could not be matched ({', '.join(labels)}); verify manually.")
    return (f"Vimeo has {len(ordered)} versions ({', '.join(labels)}); Vimeo marks {cur_label} as Current. "
            f"Only the current version is migrated (Vimeo serves the current version's files); "
            f"older versions are not migrated.")


# ------------------------------------------------------------------------ audio

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_AV_KEY = re.compile(r"/av/([^/]+)/")


def _attrs(line: str) -> dict:
    body = line.split(":", 1)[1] if ":" in line else line
    return {k: v.strip('"') for k, v in _ATTR.findall(body)}


def parse_hls_audio(manifest_text: str, base_url: str) -> list[AudioTrackInfo]:
    tracks = []
    for line in manifest_text.splitlines():
        if not line.startswith("#EXT-X-MEDIA:") or "TYPE=AUDIO" not in line:
            continue
        a = _attrs(line)
        uri = a.get("URI")
        if not uri:
            continue
        absolute = urljoin(base_url, uri)
        m = _AV_KEY.search(absolute)
        key = m.group(1) if m else absolute.split("?")[0].rstrip("/").split("/")[-2]
        lang = a.get("LANGUAGE") or ""
        tracks.append(AudioTrackInfo(
            language=lang,
            name=language_name(lang),
            vimeo_name=a.get("NAME", ""),
            channels=a.get("CHANNELS"),
            default=a.get("DEFAULT") == "YES",
            dubbed=DUBBED_FLAG in (a.get("CHARACTERISTICS") or ""),
            playlist_url=absolute,
            rendition_key=key,
        ))
    return tracks


def classify_audio(tracks: list[AudioTrackInfo]) -> AudioDiscovery:
    d = AudioDiscovery()
    for t in tracks:
        if not t.dubbed:
            d.original.append(t)
            continue
        lang = base_lang(t.language)
        if lang in d.tracks:
            d.duplicates.append(t)
        else:
            d.tracks[lang] = t
    return d


def discover_audio(api, video: dict) -> AudioDiscovery:
    hls = next((f for f in video.get("files") or [] if f.get("rendition") == "adaptive" and f.get("link")), None)
    if not hls:
        return AudioDiscovery(error="no adaptive HLS file on the video (cannot list audio renditions)")
    try:
        text, final_url = api.get_manifest(hls["link"])
    except VimeoError as exc:
        return AudioDiscovery(error=f"could not fetch HLS manifest: {exc}")
    return classify_audio(parse_hls_audio(text, final_url))


# --------------------------------------------------------------------- captions


def sniff_caption_format(text: str) -> str | None:
    head = text.lstrip("\ufeff \t\r\n")
    if head.startswith("WEBVTT"):
        return "vtt"
    if re.match(r"<\?xml|<tt[\s>]", head):
        return "ttml"
    if re.match(r"\d+\s*\r?\n\s*\d\d:\d\d:\d\d[,.]\d{3}\s*-->", head):
        return "srt"
    return None


def select_captions(raw_tracks: list[dict]) -> tuple[dict, list[str]]:
    """One caption per base language: active > not auto-generated > newest track id."""
    by_lang: dict = {}
    notes = []
    for t in raw_tracks:
        if t.get("type") not in ("captions", "subtitles") or not t.get("link"):
            continue
        lang = t.get("language") or ""
        label = f"{lang} {t.get('name') or ''} {t.get('provenance') or ''}".lower()
        info = CaptionTrackInfo(
            language=lang,
            name=language_name(lang),
            vimeo_type=t.get("type"),
            vimeo_track_id=str(t.get("id") or video_id_from_uri(t.get("uri", "/"))),
            vtt_url=t.get("link"),
            srt_url=t.get("source_link"),
            source_name=t.get("name"),
            active=bool(t.get("active", True)),
            autogen="autogen" in label or "auto-gen" in label,
        )
        by_lang.setdefault(base_lang(lang), []).append(info)
    chosen = {}
    for lang, cands in by_lang.items():
        cands.sort(key=lambda c: (c.active, not c.autogen, int(c.vimeo_track_id) if c.vimeo_track_id.isdigit() else 0),
                   reverse=True)
        chosen[lang] = cands[0]
        if len(cands) > 1:
            notes.append(f"{lang}: {len(cands)} caption tracks on Vimeo; using track {cands[0].vimeo_track_id}")
    return chosen, notes


def resolve_caption_source(api, c: CaptionTrackInfo, sniff: bool = True) -> None:
    """Prefer the original SRT (source_link) when it really is SRT; otherwise the VTT link."""
    if c.srt_url:
        fmt = api.sniff_caption(c.srt_url) if sniff else "srt"
        if fmt == "srt":
            c.source_url, c.source_format = c.srt_url, "srt"
            return
    if c.vtt_url:
        c.source_url, c.source_format = c.vtt_url, "vtt"


def discover_captions(api, vimeo_id: str, sniff: bool = True):
    chosen, notes = select_captions(api.get_texttracks(vimeo_id))
    for c in chosen.values():
        resolve_caption_source(api, c, sniff)
    return chosen, notes


# -------------------------------------------------------------------- full plan


def build_video_plan(api, video: dict, folder_name: str = "Root", sniff: bool = True) -> VideoPlan:
    info = parse_video(video, folder_name)
    if not any(f.get("rendition") == "adaptive" for f in video.get("files") or []):
        video = api.get_video(info.vimeo_id)   # list payloads can omit files[]; fetch the full object
    plan = VideoPlan(info=info, download=pick_best_download(video), audio=AudioDiscovery())

    def problem(stage, ttype, lang, msg):
        plan.problems.append({"stage": stage, "track_type": ttype, "language": lang, "message": msg})

    if plan.download is None:
        problem("DISCOVERY", None, None, "no downloadable progressive video file (files[]/download[] empty)")
    if info.status != "available" or not info.is_playable:
        problem("DISCOVERY", None, None, f"video not available/playable (status={info.status})")
    if info.transcode_status not in (None, "complete"):
        problem("DISCOVERY", None, None, f"transcode status is '{info.transcode_status}'")

    plan.audio = discover_audio(api, video)
    if plan.audio.error:
        problem("AUDIO_DISCOVERY", "audio", None, plan.audio.error)
    for lang in plan.missing_audio:
        problem("AUDIO_DISCOVERY", "audio", lang, f"no dubbed {language_name(lang)} audio rendition found")
    for t in plan.audio.duplicates:
        plan.notes.append(f"extra dubbed {t.language} audio rendition {t.rendition_key} ignored (language already covered)")

    try:
        plan.captions, plan.caption_notes = discover_captions(api, info.vimeo_id, sniff)
    except VimeoError as exc:
        problem("CAPTION_DISCOVERY", "caption", None, f"could not list text tracks: {exc}")
    for lang in plan.missing_captions:
        problem("CAPTION_DISCOVERY", "caption", lang, f"no {language_name(lang)} caption track found")
    plan.notes.extend(plan.caption_notes)

    vnote = version_note(api, info)
    if vnote:
        plan.notes.append(vnote)
    return plan
