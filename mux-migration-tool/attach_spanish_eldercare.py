"""
attach_spanish_eldercare.py
---------------------------
Attaches Spanish SRT captions + VO audio to matched Mux assets (Eldercare course).

Sources:
  SRT : D:\\new uploads\\Eldercare Spanish\\SRT\\          (160 .srt files)
  VOM : D:\\new uploads\\Eldercare Spanish\\Eldercare Spanish VOM 160-20260615T074801Z-3-002.zip (29 .mp3)

Matching: strip " Esp V4" / " VOM Esp V4" suffix from filename, match against DB vimeo_title.
Safe to re-run — skips assets already done (state persisted in logs/spanish_eldercare_state.json).

Usage:
    python attach_spanish_eldercare.py --dry-run   # preview matches, no API calls
    python attach_spanish_eldercare.py             # attach all SRT + VO
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
SRT_DIR  = Path(r"D:\new uploads\Eldercare Spanish\SRT")
VOM_DIR  = Path(r"D:\new uploads\Eldercare Spanish\VOM")

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SERVER_BASE_URL  = os.getenv("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"

LOGS_DIR   = Path(__file__).parent / "logs"
STATE_FILE = LOGS_DIR / "spanish_eldercare_state.json"
CUTOFF     = datetime(2026, 6, 1)
LANG_CODE  = "es"
LANG_NAME  = "Spanish"

# ── Direct asset ID overrides (normalized title → mux_asset_id) ───────────────
# For videos whose DB vimeo_title doesn't exactly match the SRT/VOM filename.
# Asset IDs sourced from base44 Chapter export (June 13 2026 entries).
ASSET_OVERRIDES: dict[str, str] = {
    "age related changes in an elder":
        "A00LvNw9ZkZif9z67sLeKHnYdt9NKTOF902G7YwGpwSic",
    "avoiding potential conflicts and resolving conflicts with family members":
        "OILqaNhdpYo1UR4dghB02dfmkuuQUYEd1015UW01027duKE",
    "creating a structured schedule":           # file has no "for the Elder"
        "00EONeckUvqxlSABlH1000000Eg1x3EGsxRsx1023WMmA4kU",
    "creating a structured schedule for the elder":
        "00EONeckUvqxlSABlH1000000Eg1x3EGsxRsx1023WMmA4kU",
    "first day of duty with the elders":        # file has plural "Elders"
        "MxXZ5HPpMqc8SXgVzs1g014Q4z9yR00YDZQJODtdRwSqI",
    "first day of duty with the elder":
        "MxXZ5HPpMqc8SXgVzs1g014Q4z9yR00YDZQJODtdRwSqI",
    "prepare for the day with the elder":
        "VUZxapKIG00L68UVe9pXBIQcrs91dzaVo00bRQgCzWvDc",
    "urinogenital related issues in male elders":   # file differs from DB title
        "lnB502Hl76Q4XdJ01qxNAsmxnaqRWYL3w028dH00YXPSNwE",
    "urinogenital related issues of the male":
        "lnB502Hl76Q4XdJ01qxNAsmxnaqRWYL3w028dH00YXPSNwE",
    # "Planning Mealtimes" has no matching Eldercare video in DB — skipped
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ── Suffix stripping ──────────────────────────────────────────────────────────

def strip_srt_suffix(name: str) -> str:
    """'Administering Medicines Esp V4' -> 'Administering Medicines'"""
    name = re.sub(r'\s*\(\d+\)\s*$', '', name)                      # "(1)" duplicate suffix
    name = re.sub(r'\s+New\s*$', '', name, flags=re.IGNORECASE)     # " New" variant suffix
    name = re.sub(r'\s+VO\s+Esp\s+V\d+\s*$', '', name, flags=re.IGNORECASE)  # " VO Esp V4"
    name = re.sub(r'\s+Esp\s+V\d+\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+V\d+\s*$', '', name, flags=re.IGNORECASE)    # bare " V4" fallback
    return name.strip()

def strip_vom_suffix(name: str) -> str:
    """'Caregivers Work Planner VOM Esp V4' -> 'Caregivers Work Planner'"""
    name = re.sub(r'\s+New\s*$', '', name, flags=re.IGNORECASE)     # " New" variant suffix
    name = re.sub(r'\s+VOM\s+Esp(?:añol)?\s+V\d+\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+VOM\s+ESP\s+V\d+\s*$',           '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+VOM\s+V\d+\s*$',                 '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+VOM\s*$',                         '', name, flags=re.IGNORECASE)
    return name.strip()

def normalize(s: str) -> str:
    """Lowercase + collapse whitespace + strip apostrophes/underscores for fuzzy matching.
    Handles: Elder_s == Elder's == Elders"""
    s = s.replace("_", "'")                     # underscore → apostrophe (filename convention)
    s = re.sub(r"[''']", "", s)                 # strip all apostrophe variants
    s = re.sub(r'\s+', ' ', s.lower()).strip()
    return s


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    LOGS_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Mux / server helpers ──────────────────────────────────────────────────────

def clean_srt(src_path: str) -> bytes:
    """Read SRT, strip UTF-8 BOM and/or invalid leading 'srt' header line, return clean UTF-8 bytes."""
    # utf-8-sig automatically strips BOM if present
    with open(src_path, encoding="utf-8-sig", errors="replace") as f:
        content = f.read()
    # Normalize line endings to LF
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip().lower() == "srt":
        lines = lines[1:]                          # drop the bad header
        while lines and not lines[0].strip():
            lines = lines[1:]
        log.info(f"    Stripped invalid 'srt' header line from SRT.")
    return "".join(lines).encode("utf-8")

LARGE_FILE_THRESHOLD_MB = 10  # files above this use raw-bytes endpoint to avoid multipart size limits

def serve_file(src_path: str, filename: str, is_srt: bool = False) -> str:
    if is_srt:
        data = clean_srt(src_path)
        size_mb = len(data) / 1_000_000
        log.info(f"    Uploading {filename} ({size_mb:.2f} MB) to server ...")
        r = requests.post(f"{SERVER_BASE_URL}/upload/temp-file",
                          files={"file": (filename, data, "text/plain")}, timeout=300)
        if not r.ok:
            raise RuntimeError(f"Server upload failed ({r.status_code}): {r.text[:200]}")
    else:
        size_mb = os.path.getsize(src_path) / 1_000_000
        log.info(f"    Uploading {filename} ({size_mb:.2f} MB) to server ...")
        if size_mb > LARGE_FILE_THRESHOLD_MB:
            # Use raw-bytes endpoint to bypass python-multipart size limits
            log.info(f"    (large file — using raw upload endpoint)")
            with open(src_path, "rb") as f:
                r = requests.post(
                    f"{SERVER_BASE_URL}/upload/temp-raw/{filename}",
                    data=f,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=600,
                )
        else:
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
    r = requests.post(
        f"{MUX_BASE}/assets/{asset_id}/tracks",
        json={"url": url, "type": "text", "text_type": "subtitles",
              "language_code": LANG_CODE, "name": LANG_NAME},
        auth=AUTH, timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Mux SRT attach failed ({r.status_code}): {r.text[:300]}")

def mux_add_audio(asset_id: str, url: str):
    r = requests.post(
        f"{MUX_BASE}/assets/{asset_id}/tracks",
        json={"url": url, "type": "audio",
              "language_code": LANG_CODE, "name": LANG_NAME},
        auth=AUTH, timeout=30,
    )
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
            v.audio_tracks_count = len(langs) + 1  # +1 for original track
        db.commit()
        log.info(f"    DB updated: captions={v.captions_languages} | audio={v.audio_languages}")


# ── Build match table ─────────────────────────────────────────────────────────

def load_vom_dir() -> dict[str, Path]:
    """Scan VOM_DIR for mp3 files, return {normalized_title: mp3_path}."""
    vom_map = {}
    if not VOM_DIR.exists():
        log.warning(f"VOM dir not found: {VOM_DIR}")
        return vom_map
    for mp3 in VOM_DIR.glob("*.mp3"):
        title = strip_vom_suffix(mp3.stem)
        vom_map[normalize(title)] = mp3
    log.info(f"VOM files : {len(vom_map)}")
    return vom_map


def build_matches() -> list[dict]:
    # SRT files
    srt_files = sorted(p for p in SRT_DIR.glob("*.srt"))
    srt_map: dict[str, Path] = {}
    for p in srt_files:
        title = strip_srt_suffix(p.stem)
        srt_map[normalize(title)] = p

    log.info(f"SRT files : {len(srt_map)}")

    # VOM audio
    vom_map = load_vom_dir()

    # DB videos (June 2026+, since pre-June assets were deleted)
    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.created_at > CUTOFF).all()
        title_map: dict[str, Video] = {}
        for v in videos:
            title_map[normalize(v.vimeo_title or "")] = v

    log.info(f"DB videos : {len(title_map)} (after {CUTOFF.date()})")

    unmatched_srt = []
    unmatched_vom = []

    # Build per-asset entries
    asset_map: dict[str, dict] = {}

    for norm_title, srt_path in srt_map.items():
        v = title_map.get(norm_title)
        asset_id = v.mux_asset_id if v else ASSET_OVERRIDES.get(norm_title)
        if not asset_id:
            unmatched_srt.append(srt_path.name)
            continue
        entry = asset_map.setdefault(asset_id, {"asset_id": asset_id, "video": v, "srt": None, "vom": None})
        entry["srt"] = srt_path

    for norm_title, vom_path in vom_map.items():
        v = title_map.get(norm_title)
        asset_id = v.mux_asset_id if v else ASSET_OVERRIDES.get(norm_title)
        if not asset_id:
            unmatched_vom.append(vom_path.name)
            continue
        entry = asset_map.setdefault(asset_id, {"asset_id": asset_id, "video": v, "srt": None, "vom": None})
        entry["vom"] = vom_path

    if unmatched_srt:
        log.warning(f"\n⚠️  {len(unmatched_srt)} SRT files with no DB match:")
        for f in unmatched_srt:
            log.warning(f"   {f}")

    if unmatched_vom:
        log.warning(f"\n⚠️  {len(unmatched_vom)} VOM files with no DB match:")
        for f in unmatched_vom:
            log.warning(f"   {f}")

    return list(asset_map.values())


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool):
    try:
        matches = build_matches()
        state   = load_state()

        srt_count = sum(1 for m in matches if m["srt"])
        vom_count = sum(1 for m in matches if m["vom"])

        log.info(f"\n{'═'*60}")
        log.info(f" Spanish SRT + VO Attachment — {len(matches)} assets")
        log.info(f"   SRT matched : {srt_count}")
        log.info(f"   VOM matched : {vom_count}")
        if dry_run:
            log.info(f" DRY RUN — no API calls")
        log.info(f"{'═'*60}\n")

        if dry_run:
            for i, m in enumerate(matches, 1):
                v     = m["video"]
                title = v.vimeo_title if v else f"[override] {m['asset_id'][:30]}"
                log.info(f"[{i:>3}] {title}")
                log.info(f"       SRT: {m['srt'].name if m['srt'] else '— none —'}")
                log.info(f"       VOM: {m['vom'].name if m['vom'] else '— none —'}")
            return

        done = skipped = failed = 0

        for i, m in enumerate(matches, 1):
            v   = m["video"]
            key = m["asset_id"]

            title = v.vimeo_title if v else f"[override] {key[:30]}"
            log.info(f"[{i}/{len(matches)}] {title}")
            log.info(f"    asset={key}")
            log.info(f"    SRT: {m['srt'].name if m['srt'] else '⚠️  missing'}")
            log.info(f"    VOM: {m['vom'].name if m['vom'] else '— not available'}")

            s = state.get(key, {})
            if s.get("srt_done") and (s.get("vom_done") or not m["vom"]):
                log.info(f"    ✅ Already done — skipping")
                skipped += 1
                continue

            srt_done = s.get("srt_done", False)
            vom_done = s.get("vom_done", False)
            errored  = False

            # ── SRT ───────────────────────────────────────────────────────────
            if not srt_done and m["srt"]:
                srt_filename = f"{key}_es.srt"
                try:
                    url = serve_file(str(m["srt"]), srt_filename, is_srt=True)
                    mux_add_srt(key, url)
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

            # ── VOM ───────────────────────────────────────────────────────────
            if not vom_done and m["vom"]:
                vom_filename = f"{key}_es.mp3"
                try:
                    url = serve_file(str(m["vom"]), vom_filename)
                    mux_add_audio(key, url)
                    log.info(f"    ✅ VOM attached.")
                    time.sleep(20)
                    cleanup_temp(vom_filename)
                    vom_done = True
                    state.setdefault(key, {})["vom_done"] = True
                    save_state(state)
                except Exception as e:
                    log.error(f"    ❌ VOM failed: {e}")
                    state.setdefault(key, {})["vom_error"] = str(e)
                    save_state(state)
                    errored = True

            # ── DB update ─────────────────────────────────────────────────────
            if v:
                db_update(v.id, added_caption=srt_done, added_audio=vom_done)

            if errored:
                failed += 1
            else:
                done += 1

        log.info(f"\n{'═'*60}")
        log.info(f" Done: {done}  |  Skipped: {skipped}  |  Failed: {failed}")
        log.info(f" State saved to: {STATE_FILE}")
        log.info(f"{'═'*60}")

    except Exception as e:
        log.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview matches without making any API calls")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
