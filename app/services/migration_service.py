import logging
import asyncio
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError

from app.services.vimeo_service import (
    get_video_download_url, extract_folder_path,
    get_video_captions, get_video_audio_tracks, get_vimeo_page
)
from app.services.mux_service import upload_video, add_signed_playback_id, wait_for_asset_ready, add_audio_track

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

        # --- STEP 1: Upload video + captions only ---
        logger.info(f"[Migration Worker] Step 1: Uploading video + captions to Mux...")
        mux_data = upload_video(
            video_url=download_url,
            title=title,
            captions=captions,
            audio_tracks=[]   # Audio attached separately after asset is ready
        )

        mux_asset_id = mux_data["asset_id"]
        mux_stream_url = f"https://stream.mux.com/{mux_data['playback_id']}.m3u8"

        # --- STEP 2: Wait for ready, then re-fetch fresh audio URLs and attach ---
        # Audio URLs from Vimeo HLS manifest are signed CDN URLs that expire within minutes.
        # We re-fetch them after the asset is ready so they are fresh at attachment time.
        logger.info(f"[Migration Worker] Step 2: Waiting for asset {mux_asset_id} to be ready...")
        wait_for_asset_ready(mux_asset_id)

        # Vimeo's HLS audio playlists use CDN-protected segments that Mux cannot fetch anonymously.
        # Dubbed audio migration requires downloading via yt-dlp and hosting on stable storage.
        fresh_audio_tracks = []
        logger.info(f"[Migration Worker] Audio track attachment skipped — Vimeo CDN blocks anonymous Mux fetcher.")

        # Create a separate signed playback ID for mobile-only downloads
        logger.info(f"[Migration Worker] Creating signed playback ID for mobile downloads...")
        try:
            signed_playback_id = add_signed_playback_id(mux_asset_id)
            logger.info(f"[Migration Worker] ✅ Signed playback ID created: {signed_playback_id}")
        except Exception as e:
            logger.warning(f"[Migration Worker] ⚠️ Could not create signed playback ID for {vimeo_id}: {str(e)}")
            signed_playback_id = None

        cap_count = len(captions) if captions else 0
        cap_langs = ", ".join([c.get("language") or "en" for c in captions]) if captions else None
        
        aud_count = len(fresh_audio_tracks) if fresh_audio_tracks else 0
        aud_langs = ", ".join([a.get("language") or "en" for a in fresh_audio_tracks]) if fresh_audio_tracks else None

        logger.info(f"[Migration Worker] Building Video database model...")
        video = Video(
            vimeo_id=vimeo_id,
            vimeo_title=title,
            vimeo_url=vimeo_url,
            vimeo_folder_path=folder_path,
            mux_asset_id=mux_asset_id,
            mux_playback_id=mux_data["playback_id"],
            mux_signed_playback_id=signed_playback_id,
            mux_stream_url=mux_stream_url,
            captions_count=cap_count,
            captions_languages=cap_langs,
            audio_tracks_count=aud_count,
            audio_languages=aud_langs,
            status="pending"
        )
        
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

async def run_bulk_migration(job_id: int, limit: int = None, folder_id: str = None):
    logger.info(f"[Bulk Runner] Initializing Page-by-Page Migration for Job ID: {job_id}")
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    target_limit = limit
    processed_count = 0
    url = None  
    
    # Generate the targeted Vimeo folder URL if requested
    custom_start_url = None
    if folder_id:
        custom_start_url = f"https://api.vimeo.com/me/projects/{folder_id}/videos?per_page=50"
        logger.info(f"[Bulk Runner] Targeting specific Vimeo Folder ID: {folder_id}")

    job.total_videos = target_limit if target_limit else 0
    db.commit()
    
    try:
        while True:
            db.refresh(job)
            if job.status == "cancelled":
                logger.info(f"[Bulk Runner] 🛑 Cancellation signal detected!")
                break
                
            if target_limit and processed_count >= target_limit:
                logger.info(f"[Bulk Runner] 🎯 Reached requested limit of {target_limit} new videos. Stopping.")
                break

            # Pass the custom URL into the page fetcher
            page_videos, next_url = await asyncio.to_thread(get_vimeo_page, url, custom_start_url)
            
            if not page_videos:
                logger.info("[Bulk Runner] No more videos found on Vimeo.")
                break

            existing_videos = db.query(Video.vimeo_id).all()
            migrated_ids = {v[0] for v in existing_videos}
            
            unmigrated_videos = []
            for v in page_videos:
                vimeo_id = v["uri"].split("/")[-1]
                if vimeo_id not in migrated_ids:
                    unmigrated_videos.append(v)
                    
            logger.info(f"[Bulk Runner] Page scanned: {len(page_videos)} videos found, {len(unmigrated_videos)} are new.")

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
                
                if target_limit:
                    percent_complete = round((processed_count) / target_limit * 100, 1)
                    logger.info(f"[Bulk Runner] Job Progress: {percent_complete}%")
                
                await asyncio.sleep(1) 

            if not next_url:
                logger.info("[Bulk Runner] Reached the end of the specified Vimeo Library/Folder.")
                break
                
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


async def run_folder_migration(job_id: int, folder_url: str, limit: int = None):
    db = SessionLocal()
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    try:
        # 1. Extract Folder ID from URL
        # Format: https://vimeo.com/user/141270659/folder/28548971
        folder_id = folder_url.split("/folder/")[-1].split("?")[0]
        
        # 2. Fetch all videos in that folder
        from app.services.vimeo_service import get_vimeo_folder_videos 
        all_videos = await asyncio.to_thread(get_vimeo_folder_videos, folder_id)
        
        # 3. Filter out videos already in our 'videos' table (Duplicate Prevention)
        existing_ids = {v[0] for v in db.query(Video.vimeo_id).all()}
        new_videos = [v for v in all_videos if v["uri"].split("/")[-1] not in existing_ids]
        
        # 4. Apply limit to the NEXT available videos
        to_migrate = new_videos[:limit] if limit else new_videos
        job.total_videos = len(to_migrate)
        db.commit()

        for v in to_migrate:
            db.refresh(job)
            if job.status == "cancelled": break

            vimeo_id = v["uri"].split("/")[-1]
            try:
                # Reuse process_single_video to handle Mux upload and DB save
                await asyncio.to_thread(
                    process_single_video, db, v.get("name"), v.get("link"), vimeo_id, f"Folder {folder_id}"
                )
                job.imported_videos += 1
            except Exception as e:
                logger.error(f"[Folder Migration] ❌ Failed for Vimeo ID {vimeo_id}: {str(e)}")
                job.failed_videos += 1
            db.commit()

        job.status = "completed"
    except Exception as e:
        logger.error(f"[Folder Migration] 🚨 FAILED for job {job_id}: {str(e)}")
        job.status = "failed"
    finally:
        db.commit()
        db.close()