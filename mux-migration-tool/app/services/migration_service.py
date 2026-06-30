import os
import asyncio
import logging
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError
from app.services.vimeo_service import get_video_download_url, get_video_captions
from app.services.mux_service import upload_video

logger = logging.getLogger(__name__)
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")


def _job_logger(job_id: int) -> logging.Logger:
    jlog = logging.getLogger(f"migration.job.{job_id}")
    if jlog.handlers:
        return jlog
    os.makedirs(LOGS_DIR, exist_ok=True)
    fh = logging.FileHandler(os.path.join(LOGS_DIR, f"job_{job_id}.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    fh.setLevel(logging.INFO)
    jlog.addHandler(fh)
    jlog.setLevel(logging.INFO)
    return jlog


def process_single_video(db, title: str, vimeo_url: str, vimeo_id: str,
                         folder_path: str = None, folder_name: str = None,
                         title_suffix: str = None) -> dict:
    """
    Migrates one Vimeo video to Mux.

    title_suffix  : appended to the Mux meta.title AND used as part of the DB key.
                    DB key = "{vimeo_id}_{suffix_slug}" when suffix provided, else plain vimeo_id.
                    This allows the same Vimeo video to be migrated multiple times with
                    different suffixes (e.g. re-migrating with improved settings).
                    Re-running with the SAME suffix is still idempotent (skips).

    Returns {"status": "success"|"skipped", "mux_asset_id": str}
    """
    display_title = f"{title}{title_suffix}" if title_suffix else title

    # DB key includes suffix slug so same video can be re-migrated with a new suffix
    if title_suffix:
        suffix_slug = title_suffix.strip().replace(" ", "_")
        db_vimeo_id = f"{vimeo_id}_{suffix_slug}"
    else:
        db_vimeo_id = vimeo_id

    existing = db.query(Video).filter(Video.vimeo_id == db_vimeo_id).first()
    if existing:
        logger.info(f"[Migration] {db_vimeo_id} already in DB — skipping.")
        return {"status": "skipped"}

    try:
        # Always use raw vimeo_id for Vimeo API calls
        download_url = get_video_download_url(vimeo_id)
        captions = get_video_captions(vimeo_id)

        mux_data = upload_video(
            video_url=download_url,
            title=display_title,
            captions=captions,
            audio_tracks=[],        # audio attached later via webhook (Vimeo API HLS approach)
            folder_name=folder_name,
        )

        asset_id = mux_data["asset_id"]
        playback_id = mux_data["playback_id"]           # public ID (or first available)
        signed_id = mux_data.get("signed_playback_id")  # signed ID for app JWT playback
        drm_id = mux_data.get("drm_playback_id")

        video = Video(
            vimeo_id=db_vimeo_id,
            vimeo_title=title,
            display_title=display_title,
            vimeo_url=vimeo_url,
            vimeo_folder_path=folder_path,
            source="vimeo",
            mux_asset_id=asset_id,
            mux_playback_id=playback_id,
            mux_signed_playback_id=signed_id,
            mux_drm_playback_id=drm_id,
            mux_stream_url=f"https://stream.mux.com/{playback_id}.m3u8" if playback_id else None,
            captions_count=len(captions),
            captions_languages=", ".join(c["language"] for c in captions) if captions else None,
            status="processing",
        )
        db.add(video)
        db.commit()
        logger.info(f"[Migration] ✅ {vimeo_id} → Mux {asset_id} (processing)")
        return {"status": "success", "mux_asset_id": asset_id}

    except Exception as e:
        db.rollback()
        raise e


async def run_folder_migration(job_id: int, folder_url: str, limit: int = None,
                               title_suffix: str = None):
    """
    Async runner: migrates all videos in a Vimeo folder to Mux.
    Folder URL format: https://vimeo.com/manage/folders/28548971
    title_suffix: appended to Mux title only (e.g. " (New)")
    """
    jlog = _job_logger(job_id)
    jlog.info(f"[Folder Migration] Job {job_id} | {folder_url} | suffix='{title_suffix}'")

    try:
        # Extract folder ID
        folder_id = folder_url.rstrip("/").split("/")[-1].split("?")[0]
        jlog.info(f"[Folder Migration] Folder ID: {folder_id}")

        # Fetch all videos (no DB session held during long network call)
        from app.services.vimeo_service import get_vimeo_folder_videos
        all_videos = await asyncio.to_thread(get_vimeo_folder_videos, folder_id)

        # Determine which are not yet in DB
        # When a suffix is provided the DB key is "{vimeo_id}_{suffix_slug}", so
        # we must compare against that composite key, not the raw Vimeo ID.
        suffix_slug = title_suffix.strip().replace(" ", "_") if title_suffix else None
        with SessionLocal() as db:
            existing_ids = {v[0] for v in db.query(Video.vimeo_id).all()}
            def _db_key(raw_id):
                return f"{raw_id}_{suffix_slug}" if suffix_slug else raw_id
            to_migrate = [
                item for item in all_videos
                if _db_key(item["video"]["uri"].split("/")[-1]) not in existing_ids
            ]
            if limit:
                to_migrate = to_migrate[:limit]
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.total_videos = len(to_migrate)
            db.commit()

        jlog.info(f"[Folder Migration] {len(to_migrate)} videos to migrate "
                  f"({len(all_videos) - len(to_migrate)} already in DB, skipped).")

        imported = failed = 0
        for item in to_migrate:
            # Check cancellation
            with SessionLocal() as db:
                job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                if job.status == "cancelled":
                    jlog.info(f"[Folder Migration] 🛑 Job {job_id} cancelled.")
                    return

            v = item["video"]
            vimeo_id = v["uri"].split("/")[-1]
            folder_name = item["folder_name"]
            title = v.get("name", "Untitled")
            vimeo_url = v.get("link", f"https://vimeo.com/{vimeo_id}")
            jlog.info(f"[Folder Migration] {imported + failed + 1}/{len(to_migrate)} — {vimeo_id} '{title}'")

            with SessionLocal() as db:
                try:
                    await asyncio.to_thread(
                        process_single_video,
                        db, title, vimeo_url, vimeo_id, folder_name, folder_name, title_suffix
                    )
                    imported += 1
                except Exception as e:
                    jlog.error(f"[Folder Migration] ❌ {vimeo_id}: {e}")
                    failed += 1
                    db.rollback()
                    db.add(MigrationError(job_id=job_id, vimeo_id=vimeo_id, error_message=str(e)))
                finally:
                    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                    job.imported_videos = imported
                    job.failed_videos = failed
                    db.commit()

            # Small delay between videos to avoid burst-triggering Mux rate limits.
            # upload_video() makes 3 API calls per video (asset + signed ID + public ID),
            # so even a 0.5s gap keeps us well under Mux's sustained rate limit.
            await asyncio.sleep(0.5)

        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.status = "completed"
            job.imported_videos = imported
            job.failed_videos = failed
            db.commit()
        jlog.info(f"[Folder Migration] ✅ Job {job_id} done. Imported: {imported}, Failed: {failed}")

    except Exception as e:
        jlog.error(f"[Folder Migration] 🚨 Job {job_id} crashed: {e}")
        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            if job:
                job.status = "failed"
                db.commit()
