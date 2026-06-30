"""
fix_all_tracks.py
-----------------
Single script to fix three issues across all post-June Mux assets:

  1. DELETE en-x-autogen caption track (Mux auto-generated, messy)
  2. RENAME default audio track → "English" (shows as "Default" otherwise)
  3. FIX errored Spanish SRT tracks caused by UTF-8 BOM / invalid 'srt' header:
       - Deletes the errored Spanish text track
       - Re-uploads SRT with BOM stripped + header cleaned
       - Re-attaches to Mux

Safe to re-run — state saved in logs/fix_all_tracks_state.json.

Usage:
    python fix_all_tracks.py --dry-run   # preview only
    python fix_all_tracks.py             # apply all fixes
    python fix_all_tracks.py --srt-only  # only fix Spanish SRT (skip autogen/audio rename)
"""

import os, sys, re, json, time, argparse, logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import requests
from app.database.session import SessionLocal
from app.database.models import Video

# ── Config ────────────────────────────────────────────────────────────────────
SRT_DIR  = Path(r"D:\new uploads\Eldercare Spanish\SRT")

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SERVER_BASE_URL  = os.getenv("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"

CUTOFF     = datetime(2026, 6, 1)
LOGS_DIR   = Path(__file__).parent / "logs"
STATE_FILE = LOGS_DIR / "fix_all_tracks_state.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

# Manual asset ID overrides for Spanish Eldercare titles that differ from filenames
ASSET_OVERRIDES: dict[str, str] = {
    "age related changes in an elder":
        "A00LvNw9ZkZif9z67sLeKHnYdt9NKTOF902G7YwGpwSic",
    "avoiding potential conflicts and resolving conflicts with family members":
        "OILqaNhdpYo1UR4dghB02dfmkuuQUYEd1015UW01027duKE",
    "creating a structured schedule":
        "00EONeckUvqxlSABlH1000000Eg1x3EGsxRsx1023WMmA4kU",
    "creating a structured schedule for the elder":
        "00EONeckUvqxlSABlH1000000Eg1x3EGsxRsx1023WMmA4kU",
    "first day of duty with the elders":
        "MxXZ5HPpMqc8SXgVzs1g014Q4z9yR00YDZQJODtdRwSqI",
    "first day of duty with the elder":
        "MxXZ5HPpMqc8SXgVzs1g014Q4z9yR00YDZQJODtdRwSqI",
    "prepare for the day with the elder":
        "VUZxapKIG00L68UVe9pXBIQcrs91dzaVo00bRQgCzWvDc",
    "urinogenital related issues in male elders":
        "lnB502Hl76Q4XdJ01qxNAsmxnaqRWYL3w028dH00YXPSNwE",
    "urinogenital related issues of the male":
        "lnB502Hl76Q4XdJ01qxNAsmxnaqRWYL3w028dH00YXPSNwE",
    "planning mealtimes":
        None,  # TODO: fill in asset ID — DB title is "Planning Mealtime" (no s), SRT is "Planning Mealtimes"
}


# ── SRT helpers ───────────────────────────────────────────────────────────────

def strip_srt_suffix(name: str) -> str:
    name = re.sub(r'\s*\(\d+\)\s*$', '', name)
    name = re.sub(r'\s+New\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+VO\s+Esp\s+V\d+\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+Esp\s+V\d+\s*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+V\d+\s*$', '', name, flags=re.IGNORECASE)
    return name.strip()

def normalize(s: str) -> str:
    s = s.replace("_", "'")
    s = re.sub(r"[''']", "", s)
    return re.sub(r'\s+', ' ', s.lower()).strip()

def needs_srt_repair(path: Path) -> bool:
    """Returns True if the SRT file has a BOM or an invalid 'srt' header."""
    with open(path, "rb") as f:
        raw = f.read(4)
    if raw[:3] == b"\xef\xbb\xbf":
        return True  # BOM present
    first_line = path.open(encoding="utf-8", errors="replace").readline().strip().lower()
    return first_line == "srt"

def clean_srt(src_path) -> bytes:
    """Strip UTF-8 BOM and/or invalid 'srt' header line, normalize line endings."""
    with open(src_path, encoding="utf-8-sig", errors="replace") as f:
        content = f.read()
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip().lower() == "srt":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "".join(lines).encode("utf-8")

def build_srt_repair_map(title_map: dict) -> dict[str, Path]:
    """Returns {asset_id: srt_path} for SRT files that need repair."""
    result = {}
    for srt_path in sorted(SRT_DIR.glob("*.srt")):
        if not needs_srt_repair(srt_path):
            continue
        title = strip_srt_suffix(srt_path.stem)
        norm  = normalize(title)
        v = title_map.get(norm)
        asset_id = v.mux_asset_id if v else ASSET_OVERRIDES.get(norm)
        if asset_id:
            result[asset_id] = srt_path
        else:
            log.warning(f"SRT repair — no match: {srt_path.name}")
    return result


# ── Mux helpers ───────────────────────────────────────────────────────────────

def get_tracks(asset_id):
    # GET /assets/{id}/tracks returns 501 for this account — use asset detail instead
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}", auth=AUTH, timeout=15)
    if r.status_code in (404, 501):
        return None  # asset doesn't exist on Mux — caller should skip
    if not r.ok:
        raise RuntimeError(f"GET asset ({r.status_code}): {r.text[:200]}")
    return r.json().get("data", {}).get("tracks", [])

def delete_track(asset_id, track_id):
    r = requests.delete(f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"DELETE track ({r.status_code}): {r.text[:200]}")

def update_track(asset_id, track_id, name, language_code):
    r = requests.patch(
        f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}",
        json={"name": name, "language_code": language_code},
        auth=AUTH, timeout=15,
    )
    if r.status_code == 501:
        return False  # Not supported on this account — caller will log and skip
    if not r.ok:
        raise RuntimeError(f"PATCH track ({r.status_code}): {r.text[:200]}")
    return True

def upload_srt(src_path, filename) -> str:
    data = clean_srt(src_path)
    r = requests.post(f"{SERVER_BASE_URL}/upload/temp-file",
                      files={"file": (filename, data, "text/plain")}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Upload ({r.status_code}): {r.text[:200]}")
    return r.json()["url"]

def attach_srt(asset_id, url):
    r = requests.post(
        f"{MUX_BASE}/assets/{asset_id}/tracks",
        json={"url": url, "type": "text", "text_type": "subtitles",
              "language_code": "es", "name": "Spanish"},
        auth=AUTH, timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Mux attach ({r.status_code}): {r.text[:300]}")

def cleanup_temp(filename):
    try:
        requests.delete(f"{SERVER_BASE_URL}/upload/temp-file/{filename}", timeout=10)
    except Exception:
        pass


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state():
    LOGS_DIR.mkdir(exist_ok=True)
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run: bool, srt_only: bool):
    with SessionLocal() as db:
        videos = db.query(Video).filter(
            Video.created_at > CUTOFF,
            Video.status == "ready",
            Video.mux_asset_id.isnot(None),
        ).all()

    title_map = {normalize(v.vimeo_title or ""): v for v in videos}
    srt_repair_map = build_srt_repair_map(title_map)

    state = load_state()

    log.info(f"\n{'═'*60}")
    log.info(f" Fix All Tracks — {len(videos)} assets")
    log.info(f"   SRT repairs needed : {len(srt_repair_map)}")
    if dry_run:
        log.info(" DRY RUN — no changes")
    if srt_only:
        log.info(" --srt-only mode: skipping autogen/audio rename")
    log.info(f"{'═'*60}\n")

    done = skipped = failed = 0

    for i, v in enumerate(videos, 1):
        if not v.mux_asset_id:
            continue

        key = v.mux_asset_id
        s   = state.get(key, {})

        needs_autogen = not srt_only and not s.get("autogen_deleted")
        needs_audio   = not srt_only and not s.get("audio_renamed")
        needs_srt     = key in srt_repair_map and not s.get("srt_repaired")

        if not needs_autogen and not needs_audio and not needs_srt:
            skipped += 1
            continue

        log.info(f"[{i}/{len(videos)}] {v.vimeo_title}")

        if dry_run:
            if needs_autogen: log.info("    → will delete autogen track")
            if needs_audio:   log.info("    → will rename audio → English")
            if needs_srt:     log.info(f"    → will repair SRT ({srt_repair_map[key].name})")
            continue

        errored = False

        try:
            tracks = get_tracks(key)
            if tracks is None:
                log.warning(f"    ⚠️  Asset not found on Mux — skipping")
                skipped += 1
                continue

            # 1. Delete en-x-autogen caption track
            if needs_autogen:
                autogen = [t for t in tracks
                           if t.get("type") == "text"
                           and t.get("language_code", "").startswith("en-x-autogen")]
                for t in autogen:
                    delete_track(key, t["id"])
                    log.info(f"    🗑  Deleted autogen: {t['id']}")
                if not autogen:
                    log.info(f"    ✓  No autogen track")
                state.setdefault(key, {})["autogen_deleted"] = True
                save_state(state)

            # 2. Rename default audio → English
            if needs_audio:
                tracks = get_tracks(key) or []  # refresh after delete
                default_audio = [
                    t for t in tracks
                    if t.get("type") == "audio"
                    and (not t.get("language_code") or t.get("name") in ("Default", "", None))
                ]
                for t in default_audio:
                    ok = update_track(key, t["id"], name="English", language_code="en")
                    if ok:
                        log.info(f"    ✏️  Renamed audio → English: {t['id']}")
                    else:
                        log.warning(f"    ⚠️  Audio rename not supported (501) — skipping")
                if not default_audio:
                    log.info(f"    ✓  No unnamed audio track")
                state.setdefault(key, {})["audio_renamed"] = True  # mark done regardless
                save_state(state)

            # 3. Repair errored Spanish SRT
            if needs_srt:
                srt_path = srt_repair_map[key]
                tracks = get_tracks(key) or []  # refresh
                es_tracks = [t for t in tracks
                             if t.get("type") == "text"
                             and t.get("language_code") == "es"]
                for t in es_tracks:
                    delete_track(key, t["id"])
                    log.info(f"    🗑  Deleted Spanish track ({t.get('status')}): {t['id']}")

                filename = f"{key}_es.srt"
                url = upload_srt(srt_path, filename)
                log.info(f"    ⬆️  Uploaded: {url}")
                attach_srt(key, url)
                log.info(f"    ✅ Spanish SRT re-attached.")
                time.sleep(20)
                cleanup_temp(filename)
                state.setdefault(key, {})["srt_repaired"] = True
                save_state(state)

            done += 1
            time.sleep(0.3)

        except Exception as e:
            log.error(f"    ❌ {e}")
            state.setdefault(key, {})["error"] = str(e)
            save_state(state)
            failed += 1

    log.info(f"\n{'═'*60}")
    log.info(f" Done: {done}  |  Skipped: {skipped}  |  Failed: {failed}")
    log.info(f"{'═'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--srt-only", action="store_true",
                        help="Only repair Spanish SRTs, skip autogen/audio fixes")
    args = parser.parse_args()
    main(dry_run=args.dry_run, srt_only=args.srt_only)
