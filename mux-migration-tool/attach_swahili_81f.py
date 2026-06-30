"""
attach_swahili_81f.py
---------------------
Attaches Swahili SRT + VO audio files to matched Mux assets — 81 Female batch.

- 81 SRT files + 73 VO files (8 SRT-only, no VO available)
- 9 manual overrides confirmed
- 2 videos had no DB record at check time and are now mapped by ID
- Uploads each file to server /temp via POST, passes URL to Mux, then cleans up
- Saves state to logs/swahili_81f_state.json — safe to re-run (skips already done)

Usage:
    python attach_swahili_81f.py --dry-run   # preview only, no API calls
    python attach_swahili_81f.py             # attach all SRT + VO
"""

import os, sys, re, json, time, argparse, logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import requests
from dotenv import load_dotenv
load_dotenv()

from app.database.session import SessionLocal
from app.database.models import Video

# ── Config ────────────────────────────────────────────────────────────────────
SRT_DIR = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\English SRT to Swahili 81 (81 Female)"
VO_DIR  = r"D:\new uploads\Swahili\Final Swahili Output Rendered-20260613T072713Z-3-001\Final Swahili Output Rendered\VOM Swahili 81 (81) Female)"

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SERVER_BASE_URL  = os.getenv("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"

LOGS_DIR   = Path(__file__).parent / "logs"
STATE_FILE = LOGS_DIR / "swahili_81f_state.json"
CUTOFF     = datetime(2026, 6, 1)
LANG_CODE  = "sw"
LANG_NAME  = "Swahili"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# ── Manual overrides: base title (lower) → DB id ─────────────────────────────
MANUAL_MAP: dict[str, int] = {
    # DB title has typo "Memebers" vs correct "Members"
    "avoiding potential conflicts and resolving conflicts with family members": 2122,
    # DB title has " of the Elder" appended
    "common blood and urine tests":                                             2113,
    # DB title has double space "Handling  Anxious"
    "handling anxious behaviour":                                               2096,
    # DB title is singular "Planning Mealtime"
    "planning mealtimes":                                                       2067,
    # DB title has double space in "Recreational  Activities"
    "the importance of recreational activities for the elder":                  2048,
    # DB title is "Urinogenital Issues in Female Elders"
    "urinogenital issues in elders":                                            2043,
    # DB title has correct spelling "Appliances"
    "using home applicances":                                                   2065,
    # Not found by fuzzy match — confirmed by direct DB lookup
    "age related changes in an elder":                                          2125,
    "first day of duty with the elder":                                         2103,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_state() -> dict:
    LOGS_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def base_title_srt(filename: str) -> str:
    name = Path(filename).stem
    for suffix in [" Swahili SRT", " Swahili"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return name.strip()

def base_title_vo(filename: str) -> str:
    name = Path(filename).stem.strip()
    name = re.sub(r'\s+VOM\s+Swahili\s*$', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'\s+Swahili\s+VOM\s*$', '', name, flags=re.IGNORECASE).strip()
    return name

def normalize(s: str) -> str:
    return s.lower().replace("_", "'").strip()

def serve_file(src_path: str, filename: str) -> str:
    size_mb = os.path.getsize(src_path) / 1_000_000
    log.info(f"    Uploading {filename} ({size_mb:.2f} MB) to server ...")
    with open(src_path, "rb") as f:
        r = requests.post(f"{SERVER_BASE_URL}/upload/temp-file",
                          files={"file": (filename, f)}, timeout=300)
    if not r.ok:
        raise RuntimeError(f"Server upload failed ({r.status_code}): {r.text[:200]}")
    url = r.json()["url"]
    log.info(f"    Hosted at: {url}")
    return url

def cleanup_temp(filename: str):
    try:
        requests.delete(f"{SERVER_BASE_URL}/upload/temp-file/{filename}", timeout=10)
    except Exception:
        pass

def mux_add_srt(asset_id: str, url: str):
    r = requests.post(f"{MUX_BASE}/assets/{asset_id}/tracks",
                      json={"url": url, "type": "text", "text_type": "subtitles",
                            "language_code": LANG_CODE, "name": LANG_NAME},
                      auth=AUTH, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Mux SRT attach failed ({r.status_code}): {r.text[:300]}")

def mux_add_audio(asset_id: str, url: str):
    r = requests.post(f"{MUX_BASE}/assets/{asset_id}/tracks",
                      json={"url": url, "type": "audio",
                            "language_code": LANG_CODE, "name": LANG_NAME},
                      auth=AUTH, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Mux audio attach failed ({r.status_code}): {r.text[:300]}")

def db_update(video_id: int, added_caption: bool, added_audio: bool):
    with SessionLocal() as db:
        v = db.query(Video).filter(Video.id == video_id).first()
        if not v:
            return
        if added_caption:
            langs = [l.strip() for l in (v.captions_languages or "").split(",") if l.strip()]
            if LANG_CODE not in langs:
                langs.append(LANG_CODE)
            v.captions_languages = ",".join(langs)
            v.captions_count = len(langs)
        if added_audio:
            langs = [l.strip() for l in (v.audio_languages or "").split(",") if l.strip()]
            if LANG_CODE not in langs:
                langs.append(LANG_CODE)
            v.audio_languages = ",".join(langs)
            v.audio_tracks_count = len(langs) + 1  # +1 for original audio track
        db.commit()
        log.info(f"    DB updated: captions={v.captions_languages} | audio={v.audio_languages}")


# ── Build match table ─────────────────────────────────────────────────────────

def build_matches() -> list[dict]:
    srt_files = sorted(f for f in os.listdir(SRT_DIR) if f.endswith(".srt"))
    vo_files  = sorted(f for f in os.listdir(VO_DIR)  if f.lower().endswith(".mp3"))

    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.created_at > CUTOFF).all()
        title_map: dict[str, Video] = {}
        norm_map:  dict[str, Video] = {}
        for v in videos:
            key = (v.vimeo_title or "").strip().lower()
            title_map[key] = v
            norm_map[normalize(key)] = v
        id_map: dict[int, Video] = {v.id: v for v in videos}

    def resolve(title: str) -> Video | None:
        key = title.lower()
        if key in MANUAL_MAP:
            return id_map.get(MANUAL_MAP[key])
        return title_map.get(key) or norm_map.get(normalize(title))

    asset_map: dict[str, dict] = {}

    for f in srt_files:
        title = base_title_srt(f)
        v = resolve(title)
        if v:
            entry = asset_map.setdefault(v.mux_asset_id, {"video": v, "srt": None, "vo": None})
            entry["srt"] = f
        else:
            log.warning(f"SRT NO MATCH: {title}")

    for f in vo_files:
        title = base_title_vo(f)
        v = resolve(title)
        if v:
            entry = asset_map.setdefault(v.mux_asset_id, {"video": v, "srt": None, "vo": None})
            entry["vo"] = f
        else:
            log.warning(f"VO NO MATCH: {title}")

    return list(asset_map.values())


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool):
    matches = build_matches()
    state   = load_state()

    log.info(f"\n{'═'*60}")
    log.info(f" Swahili 81F SRT + VO Attachment — {len(matches)} assets")
    if dry_run:
        log.info(f" DRY RUN — no API calls")
    log.info(f"{'═'*60}\n")

    done = skipped = failed = 0

    for i, m in enumerate(matches, 1):
        v   = m["video"]
        key = v.mux_asset_id

        log.info(f"[{i}/{len(matches)}] {v.vimeo_title}")
        log.info(f"    DB id={v.id} | asset={v.mux_asset_id}")
        log.info(f"    SRT: {m['srt'] or '⚠️  missing'}")
        log.info(f"    VO : {m['vo']  or '—  (none available)'}")

        s = state.get(key, {})
        if s.get("srt_done") and (s.get("vo_done") or not m["vo"]):
            log.info(f"    ✅ Already done — skipping")
            skipped += 1
            continue

        if dry_run:
            continue

        srt_done = s.get("srt_done", False)
        vo_done  = s.get("vo_done",  False)
        errored  = False

        # ── SRT ───────────────────────────────────────────────────────────────
        if not srt_done and m["srt"]:
            srt_path     = os.path.join(SRT_DIR, m["srt"])
            srt_filename = f"{key}_sw.srt"
            try:
                url = serve_file(srt_path, srt_filename)
                mux_add_srt(v.mux_asset_id, url)
                log.info(f"    ✅ SRT attached.")
                time.sleep(20)
                cleanup_temp(srt_filename)
                srt_done = True
                state.setdefault(key, {})["srt_done"] = True
                save_state(state)
            except Exception as e:
                log.error(f"    ❌ SRT failed: {e}")
                state.setdefault(key, {})["srt_error"] = str(e)
                save_state(state)
                errored = True

        # ── VO ────────────────────────────────────────────────────────────────
        if not vo_done and m["vo"]:
            vo_path     = os.path.join(VO_DIR, m["vo"])
            vo_filename = f"{key}_sw.mp3"
            try:
                url = serve_file(vo_path, vo_filename)
                mux_add_audio(v.mux_asset_id, url)
                log.info(f"    ✅ VO attached.")
                time.sleep(20)
                cleanup_temp(vo_filename)
                vo_done = True
                state.setdefault(key, {})["vo_done"] = True
                save_state(state)
            except Exception as e:
                log.error(f"    ❌ VO failed: {e}")
                state.setdefault(key, {})["vo_error"] = str(e)
                save_state(state)
                errored = True

        # ── DB update ─────────────────────────────────────────────────────────
        db_update(v.id, added_caption=srt_done, added_audio=vo_done)

        if errored:
            failed += 1
        else:
            done += 1

    log.info(f"\n{'═'*60}")
    log.info(f" Done: {done}  |  Skipped: {skipped}  |  Failed: {failed}")
    log.info(f"{'═'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
