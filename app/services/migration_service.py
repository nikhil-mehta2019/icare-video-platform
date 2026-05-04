import os
import logging
import asyncio
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError

from app.services.vimeo_service import (
    get_video_download_url, extract_folder_path,
    get_video_captions, get_video_audio_tracks, get_vimeo_page, get_video_metadata
)
from app.services.mux_service import upload_video, wait_for_asset_ready, add_audio_track

logging.basicConfig(level=logging.INFO, format="%(levelname)s:\t  %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOGS_DIR = os.path.join(BASE_DIR, "logs")

def _get_job_logger(job_id: int) -> logging.Logger:
    """Returns a logger that writes to logs/migration_job_{job_id}.log in addition to the root handlers."""
    job_logger = logging.getLogger(f"migration.job.{job_id}")
    if job_logger.handlers:
        return job_logger  # already set up
    os.makedirs(LOGS_DIR, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(os.path.join(LOGS_DIR, f"migration_job_{job_id}.log"), encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(logging.INFO)
    job_logger.addHandler(fh)
    job_logger.setLevel(logging.INFO)
    return job_logger

def process_single_video(db, title, vimeo_url, vimeo_id, folder_path=None, folder_name=None, title_suffix=None):
    effective_title = f"{title}{title_suffix}" if title_suffix else title
    effective_vimeo_id = f"{vimeo_id}{title_suffix}" if title_suffix else vimeo_id
    logger.info(f"[Migration Worker] Starting processing for Vimeo ID: {effective_vimeo_id} ({effective_title})")

    logger.info(f"[Migration Worker] Checking if {effective_vimeo_id} already exists in database...")
    existing = db.query(Video).filter(Video.vimeo_id == effective_vimeo_id).first()
    if existing:
        logger.info(f"[Migration Worker] Video {effective_vimeo_id} already exists. Skipping.")
        return {"status": "skipped"}

    try:
        logger.info(f"[Migration Worker] Requesting highest quality download URL for {vimeo_id}...")
        download_url = get_video_download_url(vimeo_id)

        logger.info(f"[Migration Worker] Fetching captions from Vimeo for {vimeo_id}...")
        captions = get_video_captions(vimeo_id)

        cap_count = len(captions) if captions else 0
        cap_langs = ", ".join([c.get("language") or "en" for c in captions]) if captions else None

        # --- STEP 1: Upload video + captions to Mux ---
        logger.info(f"[Migration Worker] Step 1: Uploading video + captions to Mux...")
        mux_data = upload_video(
            video_url=download_url,
            title=effective_title,
            captions=captions,
            audio_tracks=[],
            folder_name=folder_name,
        )

        mux_asset_id = mux_data["asset_id"]
        mux_playback_id = mux_data["playback_id"]
        # DRM playback ID is created at asset-creation time when DRM is configured.
        # No need to call add_signed_playback_id separately.
        drm_playback_id = mux_data.get("drm_playback_id")
        mux_stream_url = f"https://stream.mux.com/{mux_playback_id}.m3u8"

        if drm_playback_id:
            logger.info(f"[Migration Worker] ✅ DRM playback ID from upload: {drm_playback_id}")
        else:
            logger.info(f"[Migration Worker] ℹ️ No DRM playback ID — asset created without DRM.")

        # --- STEP 2: Save to DB immediately so webhooks can find and update this record ---
        # Status is "processing" — webhook will update it to "ready" when Mux finishes encoding.
        logger.info(f"[Migration Worker] Step 2: Saving record immediately with status='processing'...")
        video = Video(
            vimeo_id=effective_vimeo_id,
            vimeo_title=effective_title,
            vimeo_url=vimeo_url,
            vimeo_folder_path=folder_path,
            mux_asset_id=mux_asset_id,
            mux_playback_id=mux_playback_id,
            mux_signed_playback_id=drm_playback_id,
            mux_drm_playback_id=drm_playback_id,
            mux_stream_url=mux_stream_url,
            captions_count=cap_count,
            captions_languages=cap_langs,
            audio_tracks_count=0,
            audio_languages=None,
            status="processing"
        )
        db.add(video)
        db.commit()
        logger.info(f"[Migration Worker] ✅ Record saved. Mux webhook will update status when encoding completes.")

        return {
            "status": "success",
            "mux_asset_id": mux_asset_id,
            "mux_playback_id": mux_playback_id
        }
        
    except Exception as e:
        logger.error(f"[Migration Worker] ❌ Exception occurred while processing {vimeo_id}: {str(e)}")
        db.rollback()
        raise e

async def run_bulk_migration(job_id: int, limit: int = None, folder_id: str = None):
    jlog = _get_job_logger(job_id)
    jlog.info(f"[Bulk Runner] Initializing Page-by-Page Migration for Job ID: {job_id}")
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    target_limit = limit
    processed_count = 0
    url = None  
    
    # Generate the targeted Vimeo folder URL if requested
    custom_start_url = None
    if folder_id:
        custom_start_url = f"https://api.vimeo.com/me/projects/{folder_id}/videos?per_page=50"
        jlog.info(f"[Bulk Runner] Targeting specific Vimeo Folder ID: {folder_id}")

    job.total_videos = target_limit if target_limit else 0
    db.commit()

    try:
        while True:
            db.refresh(job)
            if job.status == "cancelled":
                jlog.info(f"[Bulk Runner] 🛑 Cancellation signal detected!")
                break

            if target_limit and processed_count >= target_limit:
                jlog.info(f"[Bulk Runner] 🎯 Reached requested limit of {target_limit} new videos. Stopping.")
                break

            page_videos, next_url = await asyncio.to_thread(get_vimeo_page, url, custom_start_url)

            if not page_videos:
                jlog.info("[Bulk Runner] No more videos found on Vimeo.")
                break

            existing_videos = db.query(Video.vimeo_id).all()
            migrated_ids = {v[0] for v in existing_videos}

            unmigrated_videos = []
            for v in page_videos:
                vimeo_id = v["uri"].split("/")[-1]
                if vimeo_id not in migrated_ids:
                    unmigrated_videos.append(v)

            jlog.info(f"[Bulk Runner] Page scanned: {len(page_videos)} videos found, {len(unmigrated_videos)} are new.")

            for v in unmigrated_videos:
                db.refresh(job)
                if job.status == "cancelled" or (target_limit and processed_count >= target_limit):
                    break

                vimeo_id = v["uri"].split("/")[-1]
                folder_path = extract_folder_path(v)
                vimeo_url = v.get("link", f"https://vimeo.com/{vimeo_id}")
                title = v.get("name", "Untitled")

                if not target_limit:
                    job.total_videos = processed_count + 1

                jlog.info(f"[Bulk Runner] --- Processing Video {processed_count + 1} (Vimeo ID: {vimeo_id}) ---")

                try:
                    result = await asyncio.to_thread(
                        process_single_video, db, title, vimeo_url, vimeo_id, folder_path
                    )

                    if result.get("status") in ["success", "skipped"]:
                        job.imported_videos += 1

                except Exception as e:
                    jlog.error(f"[Bulk Runner] ❌ Migration loop caught failure for {vimeo_id}: {str(e)}")
                    job.failed_videos += 1
                    db.add(MigrationError(job_id=job.id, vimeo_id=vimeo_id, error_message=str(e)))

                processed_count += 1
                db.commit()

                if target_limit:
                    percent_complete = round((processed_count) / target_limit * 100, 1)
                    jlog.info(f"[Bulk Runner] Job Progress: {percent_complete}%")

                await asyncio.sleep(1)

            if not next_url:
                jlog.info("[Bulk Runner] Reached the end of the specified Vimeo Library/Folder.")
                break

            url = next_url

        if job.status != "cancelled":
            job.status = "completed"
            jlog.info(f"[Bulk Runner] ✅ Bulk migration completed successfully for Job ID: {job_id}")
            db.commit()

    except Exception as e:
        jlog.error(f"[Bulk Runner] 🚨 CRITICAL FAILURE in bulk migration loop: {str(e)}")
        job.status = "failed"
        db.commit()
    finally:
        jlog.info("[Bulk Runner] Closing database session.")
        db.close()


async def run_folder_migration(job_id: int, folder_url: str, limit: int = None, title_suffix: str = None):
    jlog = _get_job_logger(job_id)
    jlog.info(f"[Folder Migration] Starting Job ID: {job_id} | folder: {folder_url}")
    try:
        # 1. Extract Folder ID from URL
        folder_id = folder_url.split("/folder/")[-1].split("?")[0]

        # 2. Fetch all videos (no DB session held during the long network call)
        from app.services.vimeo_service import get_vimeo_folder_videos
        all_videos = await asyncio.to_thread(get_vimeo_folder_videos, folder_id)

        # 3. Short-lived session to read existing IDs and set total_videos
        with SessionLocal() as db:
            existing_ids = {v[0] for v in db.query(Video.vimeo_id).all()}
            to_migrate_raw = [
                item for item in all_videos
                if (item["video"]["uri"].split("/")[-1] + (title_suffix or "")) not in existing_ids
            ]
            to_migrate = to_migrate_raw[:limit] if limit else to_migrate_raw
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.total_videos = len(to_migrate)
            db.commit()

        # 4. Process each video with its own short-lived session
        imported, failed = 0, 0
        for item in to_migrate:
            # Check cancellation
            with SessionLocal() as db:
                job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                if job.status == "cancelled":
                    jlog.info(f"[Folder Migration] 🛑 Job {job_id} cancelled.")
                    return

            v = item["video"]
            folder_name = item["folder_name"]
            vimeo_id = v["uri"].split("/")[-1]
            jlog.info(f"[Folder Migration] Processing {imported + failed + 1}/{len(to_migrate)} — Vimeo ID: {vimeo_id} ({v.get('name', 'Untitled')})")

            with SessionLocal() as db:
                try:
                    await asyncio.to_thread(
                        process_single_video, db, v.get("name"), v.get("link"), vimeo_id, folder_name, folder_name, title_suffix
                    )
                    imported += 1
                except Exception as e:
                    jlog.error(f"[Folder Migration] ❌ Failed for Vimeo ID {vimeo_id}: {str(e)}")
                    failed += 1
                    db.rollback()
                    db.add(MigrationError(job_id=job_id, vimeo_id=vimeo_id, error_message=str(e)))

                job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                job.imported_videos = imported
                job.failed_videos = failed
                db.commit()

        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.status = "completed"
            job.imported_videos = imported
            job.failed_videos = failed
            db.commit()
        jlog.info(f"[Folder Migration] ✅ Job {job_id} completed. Imported: {imported}, Failed: {failed}")

    except Exception as e:
        jlog.error(f"[Folder Migration] 🚨 FAILED for job {job_id}: {str(e)}")
        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.status = "failed"
            db.commit()


async def run_ids_migration(job_id: int, vimeo_ids: list[str]):
    """Migrates a specific list of Vimeo IDs."""
    jlog = _get_job_logger(job_id)
    jlog.info(f"[IDs Migration] Starting Job ID: {job_id} | {len(vimeo_ids)} video(s) requested")

    try:
        with SessionLocal() as db:
            existing_ids = {v[0] for v in db.query(Video.vimeo_id).all()}
            to_migrate = [vid for vid in vimeo_ids if vid not in existing_ids]
            skipped = len(vimeo_ids) - len(to_migrate)
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.total_videos = len(to_migrate)
            db.commit()

        if skipped:
            jlog.info(f"[IDs Migration] Skipping {skipped} already-migrated video(s).")

        imported, failed = 0, 0
        for i, vimeo_id in enumerate(to_migrate):
            with SessionLocal() as db:
                job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                if job.status == "cancelled":
                    jlog.info(f"[IDs Migration] 🛑 Job {job_id} cancelled.")
                    return

            jlog.info(f"[IDs Migration] Processing {i + 1}/{len(to_migrate)} — Vimeo ID: {vimeo_id}")
            try:
                title, vimeo_url = await asyncio.to_thread(get_video_metadata, vimeo_id)
            except Exception as e:
                jlog.error(f"[IDs Migration] ❌ Could not fetch metadata for {vimeo_id}: {str(e)}")
                failed += 1
                with SessionLocal() as db:
                    db.add(MigrationError(job_id=job_id, vimeo_id=vimeo_id, error_message=str(e)))
                    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                    job.imported_videos = imported
                    job.failed_videos = failed
                    db.commit()
                continue

            with SessionLocal() as db:
                try:
                    await asyncio.to_thread(
                        process_single_video, db, title, vimeo_url, vimeo_id
                    )
                    imported += 1
                except Exception as e:
                    jlog.error(f"[IDs Migration] ❌ Failed for Vimeo ID {vimeo_id}: {str(e)}")
                    failed += 1
                    db.rollback()
                    db.add(MigrationError(job_id=job_id, vimeo_id=vimeo_id, error_message=str(e)))

                job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
                job.imported_videos = imported
                job.failed_videos = failed
                db.commit()

        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.status = "completed"
            job.imported_videos = imported
            job.failed_videos = failed
            db.commit()
        jlog.info(f"[IDs Migration] ✅ Job {job_id} completed. Imported: {imported}, Failed: {failed}")

    except Exception as e:
        jlog.error(f"[IDs Migration] 🚨 FAILED for job {job_id}: {str(e)}")
        with SessionLocal() as db:
            job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
            job.status = "failed"
            db.commit()