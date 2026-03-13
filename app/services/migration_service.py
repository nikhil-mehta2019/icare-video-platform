import logging
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video
from app.services.vimeo_service import get_vimeo_videos, get_video_download_url, extract_folder_path
from app.services.mux_service import upload_video

logger = logging.getLogger(__name__)

def process_single_video(db, title, vimeo_url, vimeo_id, folder_path=None):
    """Processes a single video and safely handles duplicates and API errors."""
    existing = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    
    if existing:
        logger.info(f"Skipping duplicate video: {vimeo_id}")
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
            mux_stream_url=mux_stream_url
        )
        
        db.add(video)
        db.commit()
        
        return {"status": "success", "mux_asset_id": mux_data["asset_id"], "vimeo_id": vimeo_id}
    except Exception as e:
        logger.error(f"Failed to process video {vimeo_id}: {str(e)}")
        db.rollback()
        raise e

def run_bulk_migration(job_id: int):
    """Background task for migrating the entire Vimeo account robustly."""
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    try:
        videos = get_vimeo_videos()
        job.total_videos = len(videos)
        db.commit()

        for v in videos:
            vimeo_id = v["uri"].split("/")[-1]
            folder_path = extract_folder_path(v)
            vimeo_url = v.get("link", f"https://vimeo.com/{vimeo_id}")
            title = v.get("name", "Untitled")
            
            try:
                result = process_single_video(
                    db=db,
                    title=title,
                    vimeo_url=vimeo_url,
                    vimeo_id=vimeo_id,
                    folder_path=folder_path
                )
                if result["status"] == "success":
                    job.imported_videos += 1
            except Exception as e:
                logger.error(f"Error caught in bulk loop for {vimeo_id}: {str(e)}")
                job.failed_videos += 1
            
            # Commit after every video to save progress
            db.commit()

        job.status = "completed"
    except Exception as e:
        logger.error(f"Bulk migration failed critically: {str(e)}")
        job.status = "failed"
    finally:
        db.commit()
        db.close()