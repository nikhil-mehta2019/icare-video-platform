"""
fix_audio_names.py
------------------
Renames the default audio track → name="English", language_code="en"
for every post-June asset in the DB.

Uses PATCH /assets/{id}/tracks/{track_id} (the correct Mux endpoint).
State saved in logs/fix_audio_names_state.json — safe to re-run.

Usage:
    python fix_audio_names.py --dry-run
    python fix_audio_names.py
"""

import os, sys, json, argparse, logging, time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import requests
from app.database.session import SessionLocal
from app.database.models import Video

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"

CUTOFF     = datetime(2026, 6, 1)
LOGS_DIR   = Path(__file__).parent / "logs"
STATE_FILE = LOGS_DIR / "fix_audio_names_state.json"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


def load_state():
    LOGS_DIR.mkdir(exist_ok=True)
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def get_tracks(asset_id):
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}", auth=AUTH, timeout=15)
    if r.status_code in (404, 501):
        return None
    if not r.ok:
        raise RuntimeError(f"GET asset ({r.status_code}): {r.text[:200]}")
    return r.json().get("data", {}).get("tracks", [])

def patch_track(asset_id, track_id, name, language_code):
    r = requests.patch(
        f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}",
        json={"name": name, "language_code": language_code},
        auth=AUTH, timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"PATCH track ({r.status_code}): {r.text[:200]}")


def main(dry_run: bool):
    with SessionLocal() as db:
        videos = db.query(Video).filter(
            Video.created_at > CUTOFF,
            Video.status == "ready",
            Video.mux_asset_id.isnot(None),
        ).all()

    state = load_state()

    log.info(f"\n{'═'*60}")
    log.info(f" Fix Audio Names — {len(videos)} assets")
    if dry_run:
        log.info(" DRY RUN — no changes")
    log.info(f"{'═'*60}\n")

    done = skipped = failed = 0

    for i, v in enumerate(videos, 1):
        key = v.mux_asset_id
        if state.get(key, {}).get("done"):
            skipped += 1
            continue

        log.info(f"[{i}/{len(videos)}] {v.vimeo_title}")

        if dry_run:
            log.info("    → will rename default audio → English")
            continue

        try:
            tracks = get_tracks(key)
            if tracks is None:
                log.warning("    ⚠️  Asset not found on Mux — skipping")
                skipped += 1
                continue

            default_audio = [
                t for t in tracks
                if t.get("type") == "audio"
                and (not t.get("language_code") or t.get("name") in ("Default", "", None))
            ]

            if not default_audio:
                log.info("    ✓  No default audio track to rename")
            else:
                for t in default_audio:
                    patch_track(key, t["id"], name="English", language_code="en")
                    log.info(f"    ✏️  Renamed → English: {t['id']}")

            state.setdefault(key, {})["done"] = True
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
    args = parser.parse_args()
    main(dry_run=args.dry_run)
