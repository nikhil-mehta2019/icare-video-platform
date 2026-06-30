"""
delete_pre_june_mux_assets.py
Deletes all Mux assets for videos uploaded before June 1 2026,
then clears Mux IDs from DB.

THIS IS IRREVERSIBLE. Always dry-run first.

Usage:
    python delete_pre_june_mux_assets.py --dry-run
    python delete_pre_june_mux_assets.py
"""

import os, sys, argparse, time
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
CUTOFF           = datetime(2026, 6, 1)


def main(dry_run: bool):
    with SessionLocal() as db:
        videos = (
            db.query(Video)
            .filter(Video.created_at < CUTOFF)
            .filter(Video.mux_asset_id.isnot(None))
            .filter(Video.mux_asset_id != "")
            .order_by(Video.id)
            .all()
        )

    print(f"{'DRY RUN -- ' if dry_run else ''}Found {len(videos)} videos with Mux assets before {CUTOFF.date()}\n")

    deleted = skipped = failed = 0

    for i, v in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] {v.vimeo_title}")
        print(f"  db_id={v.id} | asset={v.mux_asset_id}")

        if dry_run:
            deleted += 1
            continue

        # Delete from Mux
        r = requests.delete(f"{MUX_BASE}/assets/{v.mux_asset_id}", auth=AUTH, timeout=15)

        if r.status_code in (200, 204):
            print(f"  Mux deleted.")
        elif r.status_code == 404:
            print(f"  Mux 404 — asset not found, clearing DB anyway.")
        else:
            print(f"  Mux error ({r.status_code}): {r.text[:100]}")
            failed += 1
            continue

        # Clear Mux IDs in DB
        with SessionLocal() as db:
            video = db.query(Video).filter(Video.id == v.id).first()
            if video:
                video.mux_asset_id           = None
                video.mux_playback_id        = None
                video.mux_signed_playback_id = None
                video.mux_drm_playback_id    = None
                video.mux_stream_url         = None
                video.status                 = "pending"
                db.commit()
        print(f"  DB cleared.")
        deleted += 1
        time.sleep(0.2)  # gentle rate limiting

    print(f"\n{'-'*60}")
    print(f"{'DRY RUN -- ' if dry_run else ''}Done:")
    print(f"  Deleted : {deleted}")
    print(f"  Failed  : {failed}")
    print(f"  Skipped : {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
