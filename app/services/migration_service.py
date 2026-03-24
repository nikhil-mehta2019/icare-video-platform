import logging
import asyncio
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError

from app.services.vimeo_service import (
    get_vimeo_videos, get_video_download_url, extract_folder_path,
    get_video_captions, get_video_audio_tracks,get_vimeo_page
)
from app.services.mux_service import upload_video

logging.basicConfig(level=logging.INFO, format="%(levelname)s:\t  %(message)s")
logger = logging.getLogger(__name__)

def process_single_video(db, title, vimeo_url, vimeo_id, folder_path=None):
    logger.info(f"[Migration Worker] Starting processing for Vimeo ID: {vimeo_id} ({title})")
    
    logger.info(f"[Migration Worker] Checking if {vimeo_id} already exists in database...")
    existing = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if existing:
        logger.info(f"[Migration Worker] Video {vimeo_id} already exists. Skipping.")
        return {"status": "skipped"}

    try:
        logger.info(f"[Migration Worker] Requesting highest quality download URL for {vimeo_id}...")
        download_url = get_video_download_url(vimeo_id)
        
        logger.info(f"[Migration Worker] Fetching captions from Vimeo for {vimeo_id}...")
        captions = get_video_captions(vimeo_id)
        if captions:
            logger.info(f"[Migration Worker] Found {len(captions)} caption(s).")
        else:
            logger.info(f"[Migration Worker] No captions found.")
            
        logger.info(f"[Migration Worker] Fetching audio tracks from Vimeo for {vimeo_id}...")
        audio_tracks = get_video_audio_tracks(vimeo_id)
        if audio_tracks:
            logger.info(f"[Migration Worker] Found {len(audio_tracks)} audio track(s).")
        else:
            logger.info(f"[Migration Worker] No alternate audio tracks found.")
        
        logger.info(f"[Migration Worker] Sending payload to Mux Service...")
        mux_data = upload_video(
            video_url=download_url, 
            title=title, 
            captions=captions, 
            audio_tracks=audio_tracks
        )
        
        mux_asset_id = mux_data["asset_id"]
        mux_stream_url = f"https://stream.mux.com/{mux_data['playback_id']}.m3u8"

        # Record intended tracks (Will be verified later by the Webhook)
        logger.info(f"[Migration Worker] Calculating intended track metadata for database...")
        cap_count = len(captions) if captions else 0
        cap_langs = ", ".join([c.get("language") or "en" for c in captions]) if captions else None
        
        aud_count = len(audio_tracks) if audio_tracks else 0
        aud_langs = ", ".join([a.get("language") or "en" for a in audio_tracks]) if audio_tracks else None

        logger.info(f"[Migration Worker] Building Video database model...")
        video = Video(
            vimeo_id=vimeo_id,
            vimeo_title=title,
            vimeo_url=vimeo_url,
            vimeo_folder_path=folder_path,
            mux_asset_id=mux_asset_id,
            mux_playback_id=mux_data["playback_id"],
            mux_stream_url=mux_stream_url,
            captions_count=cap_count,
            captions_languages=cap_langs,
            audio_tracks_count=aud_count,
            audio_languages=aud_langs,
            status="pending"
        )
        
        logger.info(f"[Migration Worker] Committing Video record to database...")
        db.add(video)
        db.commit()
        
        logger.info(f"[Migration Worker] ✅ Successfully processed Vimeo ID: {vimeo_id}")
        return {
            "status": "success",
            "mux_asset_id": mux_asset_id,
            "mux_playback_id": mux_data["playback_id"]
        }
        
    except Exception as e:
        logger.error(f"[Migration Worker] ❌ Exception occurred while processing {vimeo_id}: {str(e)}")
        db.rollback()
        raise e

async def run_bulk_migration(job_id: int, limit: int = None):
    logger.info(f"[Bulk Runner] Initializing Page-by-Page Migration for Job ID: {job_id}")
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    target_limit = limit
    processed_count = 0
    url = None  # None triggers the first page in get_vimeo_page
    
    # Pre-set total videos if a limit is provided for clean progress tracking
    job.total_videos = target_limit if target_limit else 0
    db.commit()
    
    try:
        while True:
            # 1. Stop if job was cancelled or we reached our limit target
            db.refresh(job)
            if job.status == "cancelled":
                logger.info(f"[Bulk Runner] 🛑 Cancellation signal detected!")
                break
                
            if target_limit and processed_count >= target_limit:
                logger.info(f"[Bulk Runner] 🎯 Reached requested limit of {target_limit} new videos. Stopping.")
                break

            # 2. Fetch exactly ONE page of videos from Vimeo
            page_videos, next_url = await asyncio.to_thread(get_vimeo_page, url)
            
            if not page_videos:
                logger.info("[Bulk Runner] No more videos found on Vimeo.")
                break

            # 3. Check database to see which videos in THIS page are new
            existing_videos = db.query(Video.vimeo_id).all()
            migrated_ids = {v[0] for v in existing_videos}
            
            unmigrated_videos = []
            for v in page_videos:
                vimeo_id = v["uri"].split("/")[-1]
                if vimeo_id not in migrated_ids:
                    unmigrated_videos.append(v)
                    
            logger.info(f"[Bulk Runner] Page scanned: {len(page_videos)} videos found, {len(unmigrated_videos)} are new.")

            # 4. Process ONLY the new videos found on this page
            for v in unmigrated_videos:
                db.refresh(job)
                if job.status == "cancelled" or (target_limit and processed_count >= target_limit):
                    break 

                vimeo_id = v["uri"].split("/")[-1]
                folder_path = extract_folder_path(v)
                vimeo_url = v.get("link", f"https://vimeo.com/{vimeo_id}")
                title = v.get("name", "Untitled")
                
                # If doing a full run (no limit), update total videos dynamically as we discover them
                if not target_limit:
                    job.total_videos = processed_count + 1

                logger.info(f"[Bulk Runner] --- Processing Video {processed_count + 1} (Vimeo ID: {vimeo_id}) ---")
                
                try:
                    result = await asyncio.to_thread(
                        process_single_video, db, title, vimeo_url, vimeo_id, folder_path
                    )
                    
                    if result.get("status") in ["success", "skipped"]:
                        job.imported_videos += 1
                        
                except Exception as e:
                    logger.error(f"[Bulk Runner] ❌ Migration loop caught failure for {vimeo_id}: {str(e)}")
                    job.failed_videos += 1
                    
                    error_log = MigrationError(job_id=job.id, vimeo_id=vimeo_id, error_message=str(e))
                    db.add(error_log)
                
                processed_count += 1
                db.commit()
                
                # Update visual progress
                if target_limit:
                    percent_complete = round((processed_count) / target_limit * 100, 1)
                    logger.info(f"[Bulk Runner] Job Progress: {percent_complete}%")
                
                await asyncio.sleep(1) 

            # 5. If we finished the page and there's no next page, we are done
            if not next_url:
                logger.info("[Bulk Runner] Reached the end of the entire Vimeo library.")
                break
                
            # Set up the URL for the next loop iteration
            url = next_url 

        if job.status != "cancelled":
            job.status = "completed"
            logger.info(f"[Bulk Runner] ✅ Bulk migration completed successfully for Job ID: {job_id}")
            db.commit()

    except Exception as e:
        logger.error(f"[Bulk Runner] 🚨 CRITICAL FAILURE in bulk migration loop: {str(e)}")
        job.status = "failed"
        db.commit()
    finally:
        logger.info("[Bulk Runner] Closing database session.")
        db.close()