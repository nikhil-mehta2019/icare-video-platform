"""
repair_spanish_srt.py
---------------------
Finds all Spanish SRT tracks on Mux that have status="errored" (due to BOM/invalid header),
deletes them, re-uploads the cleaned SRT, and re-attaches.

Reuses the same matching logic + clean_srt() fix from attach_spanish_eldercare.py.
State is saved to logs/repair_spanish_srt_state.json — safe to re-run.

Usage:
    python repair_spanish_srt.py --dry-run
    python repair_spanish_srt.py
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

# ── Config (same as attach_spanish_eldercare.py) ──────────────────────────────
SRT_DIR  = Path(r"D:\new uploads\Eldercare Spanish\SRT")

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
SERVER_BASE_URL  = os.getenv("SERVER_BASE_URL", "http://localhost:8000").rstrip("/")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"

CUTOFF     = datetime(2026, 6, 1)
LANG_CODE  = "es"
LANG_NAME  = "Spanish"
LOGS_DIR   = Path(__file__).parent / "logs"
STATE_FILE = LOGS_DIR / "repair_spanish_srt_state.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

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
}


# ── SRT helpers (same logic as main script) ───────────────────────────────────

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

def has_bom(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(3) == b"\xef\xbb\xbf"

def clean_srt(src_path) -> bytes:
    """Strip BOM and/or invalid 'srt' header, normalize line endings."""
    with open(src_path, encoding="utf-8-sig", errors="replace") as f:
        content = f.read()
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip().lower() == "srt":
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "".join(lines).encode("utf-8")


# ── Mux helpers ───────────────────────────────────────────────────────────────

def get_tracks(asset_id):
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}/tracks", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"GET tracks ({r.status_code}): {r.text[:200]}")
    return r.json().get("data", [])

def delete_track(asset_id, track_id):
    r = requests.delete(f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"DELETE track ({r.status_code}): {r.text[:200]}")

def upload_srt(src_path, filename) -> str:
    data = clean_srt(src_path)
    r = requests.post(f"{SERVER_BASE_URL}/upload/temp-file",
                      files={"file": (filename, data, "text/plain")}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Upload failed ({r.status_code}): {r.text[:200]}")
    return r.json()["url"]

def attach_srt(asset_id, url):
    r = requests.post(
        f"{MUX_BASE}/assets/{asset_id}/tracks",
        json={"url": url, "type": "text", "text_type": "subtitles",
              "language_code": LANG_CODE, "name": LANG_NAME},
        auth=AUTH, timeout=30,
    )
    if not r.ok:
        raise RuntimeError(f"Mux attach ({r.status_code}): {r.text[:300]}")

def cleanup_temp(filename):
    try:
        requests.delete(f"{SERVER_BASE_URL}/upload/temp-file/{filename}", timeout=10)
    except Exception:
        pass


# ── Build asset→srt_path map ─────────────────────────────────────────────────

def build_srt_map() -> dict[str, Path]:
    """Returns {asset_id: srt_path} for files that have BOM issues."""
    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.created_at > CUTOFF).all()
        title_map = {normalize(v.vimeo_title or ""): v for v in videos}

    asset_srt: dict[str, Path] = {}
    for srt_path in sorted(SRT_DIR.glob("*.srt")):
        if not has_bom(srt_path):
            # Only process BOM files — the rest were already fine
            head = srt_path.read_bytes()[:4]
            first_line = srt_path.open(encoding="utf-8", errors="replace").readline().strip().lower()
            if first_line != "srt":
                continue  # no BOM, no srt header → already clean, skip

        title = strip_srt_suffix(srt_path.stem)
        norm  = normalize(title)
        v = title_map.get(norm)
        asset_id = v.mux_asset_id if v else ASSET_OVERRIDES.get(norm)
        if asset_id:
            asset_srt[asset_id] = srt_path

    return asset_srt


# ── Main ─────────────────────────────────────────────────────────────────────

def main(dry_run):
    asset_srt = build_srt_map()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    LOGS_DIR.mkdir(exist_ok=True)

    log.info(f"\n{'═'*60}")
    log.info(f" Repair Spanish SRT — {len(asset_srt)} assets with BOM issues")
    if dry_run:
        log.info(" DRY RUN")
    log.info(f"{'═'*60}\n")

    done = skipped = failed = 0

    for i, (asset_id, srt_path) in enumerate(asset_srt.items(), 1):
        if state.get(asset_id, {}).get("repaired"):
            log.info(f"[{i}/{len(asset_srt)}] ✅ Already repaired — skip  ({srt_path.name})")
            skipped += 1
            continue

        log.info(f"[{i}/{len(asset_srt)}] {srt_path.name}")
        log.info(f"    asset={asset_id}")
        log.info(f"    BOM={has_bom(srt_path)}")

        if dry_run:
            continue

        try:
            tracks = get_tracks(asset_id)
            es_tracks = [t for t in tracks
                         if t.get("type") == "text" and t.get("language_code") == LANG_CODE]

            # Delete errored (or any existing) Spanish text track
            for t in es_tracks:
                delete_track(asset_id, t["id"])
                log.info(f"    🗑  Deleted track {t['id']} (status={t.get('status')})")

            # Re-upload cleaned SRT and attach
            filename = f"{asset_id}_es.srt"
            url = upload_srt(srt_path, filename)
            log.info(f"    ⬆️  Uploaded to {url}")
            attach_srt(asset_id, url)
            log.info(f"    ✅ SRT re-attached.")
            time.sleep(20)
            cleanup_temp(filename)

            state.setdefault(asset_id, {})["repaired"] = True
            STATE_FILE.write_text(json.dumps(state, indent=2))
            done += 1

        except Exception as e:
            log.error(f"    ❌ {e}")
            state.setdefault(asset_id, {})["error"] = str(e)
            STATE_FILE.write_text(json.dumps(state, indent=2))
            failed += 1

    log.info(f"\n{'═'*60}")
    log.info(f" Done: {done}  |  Skipped: {skipped}  |  Failed: {failed}")
    log.info(f"{'═'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
