"""
fix_mux_tracks.py
-----------------
For every video in the DB (post-June cutoff):
  1. Deletes the `en-x-autogen` caption track (Mux auto-generated)
  2. Renames the default audio track to "English" (language_code: en)

Safe to re-run — skips assets already fixed (state in logs/fix_tracks_state.json).

Usage:
    python fix_mux_tracks.py --dry-run   # preview only
    python fix_mux_tracks.py             # apply fixes
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
STATE_FILE = LOGS_DIR / "fix_tracks_state.json"

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
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}/tracks", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"GET tracks failed ({r.status_code}): {r.text[:200]}")
    return r.json().get("data", [])

def delete_track(asset_id, track_id):
    r = requests.delete(f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"DELETE track failed ({r.status_code}): {r.text[:200]}")

def update_track(asset_id, track_id, name, language_code):
    r = requests.put(
        f"{MUX_BASE}/assets/{asset_id}/tracks/{track_id}",
        json={"name": name, "language_code": language_code},
        auth=AUTH, timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"PUT track failed ({r.status_code}): {r.text[:200]}")


def main(dry_run):
    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.created_at > CUTOFF).all()

    state = load_state()
    log.info(f"\n{'═'*60}")
    log.info(f" Fix Mux Tracks — {len(videos)} assets")
    if dry_run:
        log.info(" DRY RUN — no changes")
    log.info(f"{'═'*60}\n")

    done = skipped = failed = 0

    for i, v in enumerate(videos, 1):
        if not v.mux_asset_id:
            continue

        key = v.mux_asset_id
        s   = state.get(key, {})

        if s.get("autogen_deleted") and s.get("audio_renamed"):
            skipped += 1
            continue

        log.info(f"[{i}/{len(videos)}] {v.vimeo_title}")

        if dry_run:
            continue

        errored = False
        try:
            tracks = get_tracks(key)

            # 1. Delete en-x-autogen caption track
            if not s.get("autogen_deleted"):
                autogen = [t for t in tracks
                           if t.get("type") == "text"
                           and t.get("language_code", "").startswith("en-x-autogen")]
                if autogen:
                    for t in autogen:
                        delete_track(key, t["id"])
                        log.info(f"    🗑  Deleted autogen track: {t['id']}")
                else:
                    log.info(f"    ✓  No autogen track found")
                state.setdefault(key, {})["autogen_deleted"] = True
                save_state(state)

            # 2. Rename default audio track → English
            if not s.get("audio_renamed"):
                # Re-fetch tracks in case list changed
                tracks = get_tracks(key)
                default_audio = [
                    t for t in tracks
                    if t.get("type") == "audio"
                    and (t.get("name") in ("Default", "", None)
                         or not t.get("language_code"))
                ]
                if default_audio:
                    t = default_audio[0]
                    update_track(key, t["id"], name="English", language_code="en")
                    log.info(f"    ✏️  Renamed audio track {t['id']} → English")
                else:
                    log.info(f"    ✓  Default audio track already named / not found")
                state.setdefault(key, {})["audio_renamed"] = True
                save_state(state)

            done += 1
            time.sleep(0.3)  # gentle rate limiting

        except Exception as e:
            log.error(f"    ❌ {e}")
            state.setdefault(key, {})["error"] = str(e)
            save_state(state)
            errored = True
            failed += 1

    log.info(f"\n{'═'*60}")
    log.info(f" Done: {done}  |  Skipped: {skipped}  |  Failed: {failed}")
    log.info(f"{'═'*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
