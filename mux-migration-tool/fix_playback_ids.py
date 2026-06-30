"""
fix_playback_ids.py
-------------------
Adds a 'signed' playback policy to Mux assets that only have a DRM playback ID,
then updates mux_playback_id in the DB.

Usage:
    python fix_playback_ids.py --dry-run                    # preview only
    python fix_playback_ids.py --title-suffix " (New)"      # fix today's 160 assets
    python fix_playback_ids.py                              # fix ALL assets missing signed ID
"""

import argparse
import logging
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET
from app.database.session import SessionLocal
from app.database.models import Video

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s | %(message)s")
logger = logging.getLogger("fix_playback_ids")

AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
BASE_URL = "https://api.mux.com/video/v1"


def add_signed_playback_id(asset_id: str) -> str | None:
    """Adds a signed playback policy to a Mux asset. Returns the new playback ID."""
    import time
    for attempt in range(5):
        r = requests.post(
            f"{BASE_URL}/assets/{asset_id}/playback-ids",
            json={"policy": "signed"},
            auth=AUTH,
        )
        if r.status_code == 429:
            wait = 10 * (attempt + 1)
            logger.warning(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
            continue
        if not r.ok:
            logger.error(f"  Mux error ({r.status_code}): {r.text[:200]}")
            return None
        return r.json()["data"]["id"]
    logger.error(f"  Failed after 5 retries (rate limit)")
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--title-suffix", type=str, default=None)
    args = parser.parse_args()

    with SessionLocal() as db:
        q = db.query(Video).filter(
            Video.mux_asset_id.isnot(None),
            Video.mux_drm_playback_id.isnot(None),
            # Missing a signed/public playback ID (playback_id same as drm = no separate signed)
            (Video.mux_playback_id == None) | (Video.mux_playback_id == Video.mux_drm_playback_id),
        )
        if args.title_suffix:
            q = q.filter(Video.display_title.like(f"%{args.title_suffix}"))

        videos = q.all()
        logger.info(f"Found {len(videos)} assets needing a signed playback ID.")

        if args.dry_run:
            for v in videos:
                print(f"  {v.mux_asset_id} | {v.display_title or v.vimeo_title}")
            return

        success, failed = 0, 0
        for i, v in enumerate(videos, 1):
            logger.info(f"[{i}/{len(videos)}] {v.vimeo_id} — {v.display_title or v.vimeo_title}")
            signed_id = add_signed_playback_id(v.mux_asset_id)
            if signed_id:
                v.mux_playback_id = signed_id
                db.commit()
                logger.info(f"  ✅ signed playback ID: {signed_id}")
                success += 1
            else:
                failed += 1

        logger.info(f"\nDone. Fixed: {success} | Failed: {failed}")


if __name__ == "__main__":
    main()
