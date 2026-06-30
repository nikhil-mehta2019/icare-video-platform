"""
retry_audio.py
--------------
Re-runs audio track attachment for videos that have no audio tracks attached.
Run AFTER installing yt-dlp[default]:
    pip install "yt-dlp[default]" --break-system-packages

Usage:
    python retry_audio.py                            # retry all with audio_tracks_count < 2
    python retry_audio.py --dry-run                  # show what would be retried
    python retry_audio.py --limit 10                 # retry first 10 only
    python retry_audio.py --vimeo-id 1171823237      # retry one specific video
    python retry_audio.py --force                    # retry ALL ready videos
    python retry_audio.py --force --title-suffix " (New)"  # only videos ending with ' (New)'
"""

import argparse
import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import DATABASE_URL
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.audio_service import attach_audio_tracks_background

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("retry_audio")


def get_videos_needing_audio(limit=None, vimeo_id=None, force=False, title_suffix=None):
    with SessionLocal() as db:
        q = db.query(Video).filter(
            Video.mux_asset_id.isnot(None),
            Video.status == "ready",
        )
        if vimeo_id:
            q = q.filter(Video.vimeo_id == vimeo_id)
        elif not force:
            q = q.filter(
                (Video.audio_tracks_count == None) | (Video.audio_tracks_count < 2)
            )
        if title_suffix:
            q = q.filter(Video.display_title.like(f"%{title_suffix}"))
        if limit:
            q = q.limit(limit)
        return [
            {
                "vimeo_id": v.vimeo_id,
                "mux_asset_id": v.mux_asset_id,
                "vimeo_url": v.vimeo_url or f"https://vimeo.com/{v.vimeo_id}",
                "title": v.vimeo_title,
            }
            for v in q.all()
        ]


async def retry_all(videos):
    total = len(videos)
    success, skipped = 0, 0

    for i, v in enumerate(videos, 1):
        # Strip suffix from vimeo_id for yt-dlp (suffix is DB-only, not a real Vimeo ID)
        raw_vimeo_id = v["vimeo_id"].split("_")[0] if "_" in v["vimeo_id"] else v["vimeo_id"]
        logger.info(f"[{i}/{total}] {v['vimeo_id']} — {v['title']}")
        try:
            attached = await attach_audio_tracks_background(
                mux_asset_id=v["mux_asset_id"],
                vimeo_id=raw_vimeo_id,
                vimeo_url=v["vimeo_url"],
            )
            # Update DB with result
            with SessionLocal() as db:
                from sqlalchemy import text
                db.execute(
                    text("UPDATE videos SET audio_tracks_count=:c, audio_languages=:l WHERE vimeo_id=:vid"),
                    {"c": len(attached), "l": ", ".join(attached) if attached else None, "vid": v["vimeo_id"]}
                )
                db.commit()

            if attached:
                logger.info(f"  Attached: {attached}")
                success += 1
            else:
                logger.info(f"  No alternate audio found")
                skipped += 1
        except Exception as e:
            logger.error(f"  Error: {e}")
            skipped += 1

    logger.info(f"\nDone. Attached: {success} | Skipped: {skipped}")


def main():
    parser = argparse.ArgumentParser(description="Retry audio attachment for migrated videos")
    parser.add_argument("--dry-run", action="store_true", help="List videos without retrying")
    parser.add_argument("--limit", type=int, default=None, help="Max videos to process")
    parser.add_argument("--vimeo-id", type=str, default=None, help="Retry one specific Vimeo ID")
    parser.add_argument("--force", action="store_true", help="Retry all ready videos regardless of audio_tracks_count")
    parser.add_argument("--title-suffix", type=str, default=None, help="Only videos whose title ends with this suffix")
    args = parser.parse_args()

    videos = get_videos_needing_audio(
        limit=args.limit,
        vimeo_id=args.vimeo_id,
        force=args.force,
        title_suffix=args.title_suffix,
    )

    if not videos:
        logger.info("No videos found needing audio retry.")
        return

    logger.info(f"Found {len(videos)} video(s) to process.")

    if args.dry_run:
        for v in videos:
            print(f"  {v['vimeo_id']}  {v['title']}")
        return

    asyncio.run(retry_all(videos))


if __name__ == "__main__":
    main()
