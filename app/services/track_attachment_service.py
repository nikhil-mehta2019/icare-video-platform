import os
import re
import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from sqlalchemy import text

from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET, SERVER_BASE_URL
from app.database.session import SessionLocal

logger = logging.getLogger(__name__)

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, "temp_audio")
LOGS_DIR       = os.path.join(BASE_DIR, "logs")

MUX_BASE = "https://api.mux.com/video/v1"
MUX_AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)

CLEANUP_DELAY_SECONDS = 20
LARGE_FILE_MB         = 10


# ── Normalization ─────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    return s.lower().replace("_", "'").strip()

def _stem(filename: str) -> str:
    return Path(filename).stem.strip()

def _strip_srt_suffix(stem: str) -> str:
    for suffix in [" Swahili SRT", " Swahili", " Hindi SRT", " Hindi",
                   " Spanish SRT", " Spanish"]:
        if stem.endswith(suffix):
            return stem[:-len(suffix)].strip()
    return stem

def _strip_vo_suffix(stem: str) -> str:
    stem = re.sub(r'\s+VOM\s+\w+\s*$',    '', stem, flags=re.IGNORECASE).strip()
    stem = re.sub(r'\s+\w+\s+VOM\s*$',    '', stem, flags=re.IGNORECASE).strip()
    stem = re.sub(r'\s+VO\s*$',            '', stem, flags=re.IGNORECASE).strip()
    return stem

def _base_title(filename: str, file_type: str) -> str:
    stem = _stem(filename)
    if file_type == "srt":
        return _strip_srt_suffix(stem)
    return _strip_vo_suffix(stem)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_videos(cutoff: datetime | None) -> tuple[dict, dict, dict]:
    with SessionLocal() as db:
        from app.database.models import Video
        q = db.query(Video)
        if cutoff:
            q = q.filter(Video.created_at > cutoff)
        videos = q.all()
        title_map = {(v.vimeo_title or "").strip().lower(): v for v in videos}
        norm_map  = {_normalize((v.vimeo_title or "").strip().lower()): v for v in videos}
        id_map    = {v.id: v for v in videos}
    return title_map, norm_map, id_map

def _resolve(title: str, title_map: dict, norm_map: dict, id_map: dict, manual_map: dict):
    key = title.lower()
    if key in manual_map:
        return id_map.get(manual_map[key])
    return title_map.get(key) or norm_map.get(_normalize(key))

def _db_update(video_id: int, lang_code: str, added_caption: bool, added_audio: bool):
    try:
        with SessionLocal() as db:
            from app.database.models import Video
            v = db.query(Video).filter(Video.id == video_id).first()
            if not v:
                return
            if added_caption:
                langs = [l.strip() for l in (v.captions_languages or "").split(",") if l.strip()]
                if lang_code not in langs:
                    langs.append(lang_code)
                v.captions_languages = ",".join(langs)
                v.captions_count = len(langs)
            if added_audio:
                langs = [l.strip() for l in (v.audio_languages or "").split(",") if l.strip()]
                if lang_code not in langs:
                    langs.append(lang_code)
                v.audio_languages = ",".join(langs)
                v.audio_tracks_count = len(langs) + 1  # +1 for original
            db.commit()
    except Exception as e:
        logger.error(f"[TrackAttach] DB update failed for video {video_id}: {e}")


# ── File serving ──────────────────────────────────────────────────────────────

def _serve_file(src_path: str, filename: str) -> str:
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    dest = os.path.join(TEMP_AUDIO_DIR, filename)
    size_mb = os.path.getsize(src_path) / 1_000_000
    logger.info(f"[TrackAttach] Copying {filename} ({size_mb:.2f} MB) to temp ...")
    import shutil
    shutil.copy2(src_path, dest)
    url = f"{SERVER_BASE_URL}/temp-audio/{filename}"
    logger.info(f"[TrackAttach] Serving at: {url}")
    return url

def _cleanup(filename: str):
    path = os.path.join(TEMP_AUDIO_DIR, filename)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ── Mux API ───────────────────────────────────────────────────────────────────

def _mux_attach(asset_id: str, url: str, track_type: str, lang_code: str, lang_name: str):
    payload = {"url": url, "type": track_type, "language_code": lang_code, "name": lang_name}
    if track_type == "text":
        payload["text_type"] = "subtitles"
    r = requests.post(f"{MUX_BASE}/assets/{asset_id}/tracks",
                      json=payload, auth=MUX_AUTH, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Mux attach failed ({r.status_code}): {r.text[:300]}")


# ── State persistence ─────────────────────────────────────────────────────────

def _state_path(job_id: str) -> Path:
    return Path(LOGS_DIR) / f"track_attach_{job_id}.json"

def _load_state(job_id: str) -> dict:
    p = _state_path(job_id)
    return json.loads(p.read_text()) if p.exists() else {}

def _save_state(job_id: str, state: dict):
    Path(LOGS_DIR).mkdir(exist_ok=True)
    _state_path(job_id).write_text(json.dumps(state, indent=2))


# ── Match builder ─────────────────────────────────────────────────────────────

def _build_matches(
    srt_dir: str | None,
    audio_dir: str | None,
    cutoff: datetime | None,
    manual_map: dict[str, int],
    srt_ext: str = ".srt",
    audio_ext: str = ".mp3",
) -> list[dict]:
    title_map, norm_map, id_map = _load_videos(cutoff)
    asset_map: dict[str, dict] = {}

    if srt_dir and os.path.isdir(srt_dir):
        for f in sorted(os.listdir(srt_dir)):
            if not f.lower().endswith(srt_ext):
                continue
            title = _base_title(f, "srt")
            v = _resolve(title, title_map, norm_map, id_map, manual_map)
            if v and v.mux_asset_id:
                asset_map.setdefault(v.mux_asset_id, {"video": v, "srt": None, "audio": None})["srt"] = f
            else:
                logger.warning(f"[TrackAttach] SRT no match: {title}")

    if audio_dir and os.path.isdir(audio_dir):
        for f in sorted(os.listdir(audio_dir)):
            if not f.lower().endswith(audio_ext):
                continue
            title = _base_title(f, "audio")
            v = _resolve(title, title_map, norm_map, id_map, manual_map)
            if v and v.mux_asset_id:
                asset_map.setdefault(v.mux_asset_id, {"video": v, "srt": None, "audio": None})["audio"] = f
            else:
                logger.warning(f"[TrackAttach] Audio no match: {title}")

    return list(asset_map.values())


# ── Main background task ──────────────────────────────────────────────────────

def run_track_attachment(
    job_id: str,
    srt_dir: str | None,
    audio_dir: str | None,
    lang_code: str,
    lang_name: str,
    cutoff: datetime | None,
    manual_map: dict[str, int],
    dry_run: bool,
    progress: dict,
):
    progress.update({"status": "running", "done": 0, "skipped": 0, "failed": 0, "total": 0, "current": ""})

    matches = _build_matches(srt_dir, audio_dir, cutoff, manual_map)
    state   = _load_state(job_id)

    progress["total"] = len(matches)
    logger.info(f"[TrackAttach:{job_id}] {len(matches)} assets matched. dry_run={dry_run}")

    done = skipped = failed = 0

    for i, m in enumerate(matches, 1):
        v   = m["video"]
        key = v.mux_asset_id
        progress["current"] = v.vimeo_title or key

        logger.info(f"[TrackAttach:{job_id}] [{i}/{len(matches)}] {v.vimeo_title}")
        logger.info(f"  asset={key} | SRT={m['srt'] or 'none'} | audio={m['audio'] or 'none'}")

        s = state.get(key, {})
        srt_needed   = bool(m["srt"])   and not s.get("srt_done")
        audio_needed = bool(m["audio"]) and not s.get("audio_done")

        if not srt_needed and not audio_needed:
            logger.info(f"  ✅ Already done — skipping")
            skipped += 1
            progress["skipped"] = skipped
            continue

        if dry_run:
            continue

        srt_done = s.get("srt_done", False)
        audio_done = s.get("audio_done", False)
        errored = False

        # SRT
        if srt_needed:
            src      = os.path.join(srt_dir, m["srt"])
            filename = f"{key}_{lang_code}.srt"
            try:
                url = _serve_file(src, filename)
                _mux_attach(key, url, "text", lang_code, lang_name)
                logger.info(f"  ✅ SRT attached.")
                time.sleep(CLEANUP_DELAY_SECONDS)
                _cleanup(filename)
                srt_done = True
                state.setdefault(key, {})["srt_done"] = True
                _save_state(job_id, state)
            except Exception as e:
                logger.error(f"  ❌ SRT failed: {e}")
                state.setdefault(key, {})["srt_error"] = str(e)
                _save_state(job_id, state)
                errored = True

        # Audio
        if audio_needed:
            src      = os.path.join(audio_dir, m["audio"])
            filename = f"{key}_{lang_code}.mp3"
            try:
                url = _serve_file(src, filename)
                _mux_attach(key, url, "audio", lang_code, lang_name)
                logger.info(f"  ✅ Audio attached.")
                time.sleep(CLEANUP_DELAY_SECONDS)
                _cleanup(filename)
                audio_done = True
                state.setdefault(key, {})["audio_done"] = True
                _save_state(job_id, state)
            except Exception as e:
                logger.error(f"  ❌ Audio failed: {e}")
                state.setdefault(key, {})["audio_error"] = str(e)
                _save_state(job_id, state)
                errored = True

        _db_update(v.id, lang_code, added_caption=srt_done, added_audio=audio_done)

        if errored:
            failed += 1
        else:
            done += 1

        progress.update({"done": done, "skipped": skipped, "failed": failed})

    progress.update({"status": "done", "done": done, "skipped": skipped, "failed": failed})
    logger.info(f"[TrackAttach:{job_id}] DONE — done={done} skipped={skipped} failed={failed}")
