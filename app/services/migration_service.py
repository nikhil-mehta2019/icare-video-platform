import logging
import asyncio
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError
from app.services.vimeo_service import get_vimeo_videos, get_video_download_url, extract_folder_path
from app.services.mux_service import upload_video

# Force logging to display in standard output
logging.basicConfig(level=logging.INFO, format="%(levelname)s:\t  %(message)s")
logger = logging.getLogger(__name__)

def process_single_video(db, title, vimeo_url, vimeo_id, folder_path=None):
    """Processes a single video and safely handles duplicates and API errors."""
    existing = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    
    if existing:
        return {"status": "skipped", "message": "Video already imported", "vimeo_id": vimeo_id}

    try:
        download_url = get_video_download_url(vimeo_id)
        mux_data = upload_video(download_url)
        mux_stream_url = f"https://stream.mux.com/{mux_data['playback_id']}.m3u8"

        video = Video(
            vimeo_id=vimeo_id,
            vimeo_title=title,
            vimeo_url=vimeo_url,
            vimeo_folder_path=folder_path,
            mux_asset_id=mux_data["asset_id"],
            mux_playback_id=mux_data["playback_id"],
            mux_stream_url=mux_stream_url,
            status="pending"  # Will be updated by webhook
        )
        
        db.add(video)
        db.commit()
        
        return {"status": "success", "mux_asset_id": mux_data["asset_id"], "vimeo_id": vimeo_id}
    except Exception as e:
        db.rollback()
        raise e

async def run_bulk_migration(job_id: int, limit: int = None):
    """Async background task for migrating the entire Vimeo account robustly."""
    logger.info(f"Starting migration for Job ID: {job_id}")
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    try:
        logger.info("Fetching Vimeo videos...")
        # Run blocking network call in a separate thread so it doesn't freeze FastAPI
        videos = await asyncio.to_thread(get_vimeo_videos,limit)
        
        if limit and limit > 0:
            videos = videos[:limit]
            
        total = len(videos)
        job.total_videos = total
        db.commit()
        
        logger.info(f"Total videos discovered: {total}")

        for index, v in enumerate(videos, start=1):
            db.refresh(job)
            if job.status == "cancelled":
                logger.info(f"Migration Job {job.id} was stopped by the user.")
                break 

            vimeo_id = v["uri"].split("/")[-1]
            folder_path = extract_folder_path(v)
            vimeo_url = v.get("link", f"https://vimeo.com/{vimeo_id}")
            title = v.get("name", "Untitled")
            
            logger.info(f"Uploading video {index} / {total} (Vimeo ID: {vimeo_id})")
            
            try:
                # Wrap the synchronous video processing in a thread
                result = await asyncio.to_thread(
                    process_single_video, db, title, vimeo_url, vimeo_id, folder_path
                )
                
                if result["status"] == "success":
                    job.imported_videos += 1
                    logger.info(f"Mux asset creation result: SUCCESS (Asset ID: {result.get('mux_asset_id')})")
                elif result["status"] == "skipped":
                    # Count skipped as imported so percent_complete reaches 100%
                    job.imported_videos += 1 
                    logger.info(f"Mux asset creation result: SKIPPED (Already exists in database)")
                    
            except Exception as e:
                logger.error(f"Mux asset creation result: FAILED (Reason: {str(e)})")
                job.failed_videos += 1
                
                error_log = MigrationError(
                    job_id=job.id,
                    vimeo_id=vimeo_id,
                    error_message=str(e)
                )
                db.add(error_log)
            
            # Commit the progress fields to the DB immediately
            db.commit()
            
            percent_complete = round((job.imported_videos + job.failed_videos) / total * 100, 1) if total > 0 else 0
            logger.info(f"Migration Progress: {percent_complete}%")
            
            # Non-blocking pause for rate limit protection
            await asyncio.sleep(1) 

        if job.status != "cancelled":
            job.status = "completed"
            logger.info(f"Migration completed successfully for Job ID: {job_id}")
            db.commit()

    except Exception as e:
        logger.error(f"Bulk migration failed critically: {str(e)}")
        job.status = "failed"
        db.commit()
    finally:
        db.close()