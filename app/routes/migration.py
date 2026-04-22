import asyncio
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import MigrationJob, MigrationError
from app.services.migration_service import run_bulk_migration
from app.services.report_service import generate_migration_excel
from app.schemas.response_models import MigrationResponse
from app.schemas.request_models import BulkMigrationRequest
from app.services.mux_service import get_all_assets, delete_asset, add_public_playback_id, add_signed_playback_id
from app.services.migration_service import process_single_video
from app.services.audio_service import attach_audio_tracks_background
from typing import Optional
from app.services.migration_service import run_folder_migration, run_ids_migration
from typing import List

router = APIRouter(prefix="/migration", tags=["Migration"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/vimeo-account", response_model=MigrationResponse)
async def start_migration(
    background_tasks: BackgroundTasks,
    request: BulkMigrationRequest = BulkMigrationRequest(), # Defaults to no limit
    db: Session = Depends(get_db)
):
    # Migration Lock / Idempotency Check
    existing_job = db.query(MigrationJob).filter(MigrationJob.status == "running").first()
    if existing_job:
        raise HTTPException(
            status_code=400, 
            detail=f"A migration is already in progress (Job ID: {existing_job.id}). Please wait for it to complete."
        )

    job = MigrationJob()
    db.add(job)
    db.commit()
    db.refresh(job)

    # Replaced BackgroundTasks with standard asyncio.create_task for reliable async execution
    #asyncio.create_task(run_bulk_migration(job.id, request.limit))
    background_tasks.add_task(run_bulk_migration, job.id, request.limit)

    return {"status": "Migration started", "job_id": job.id}

@router.get("/export")
def export_migration_report():
    excel_file = generate_migration_excel()
    
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=vimeo_mux_mapping.xlsx"}
    )

@router.get("/errors/{job_id}")
def get_migration_errors(job_id: int, db: Session = Depends(get_db)):
    """Return all failed videos for a migration job."""
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Migration job not found")
    errors = db.query(MigrationError).filter(MigrationError.job_id == job_id).all()
    return {
        "job_id": job_id,
        "failed_count": len(errors),
        "errors": [
            {
                "vimeo_id": e.vimeo_id,
                "error_message": e.error_message,
                "failed_at": e.created_at.strftime("%Y-%m-%d %H:%M:%S") if e.created_at else None,
            }
            for e in errors
        ],
    }

@router.get("/status/{job_id}")
def get_migration_status(job_id: int, db: Session = Depends(get_db)):
    """Check the real-time progress of a specific migration job."""
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    if not job:
        raise HTTPException(status_code=404, detail="Migration job not found")
        
    return {
        "job_id": job.id,
        "status": job.status,           # "running", "completed", or "failed"
        "total_videos": job.total_videos,
        "imported_videos": job.imported_videos,
        "failed_videos": job.failed_videos,
        "percent_complete": round((job.imported_videos + job.failed_videos) / job.total_videos * 100, 1) if job.total_videos > 0 else 0
    }

@router.post("/{job_id}/cancel")
def cancel_migration(job_id: int, db: Session = Depends(get_db)):
    """Stops an active migration job by updating its status."""
    job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
    
    if not job:
        raise HTTPException(status_code=404, detail="Migration job not found")
        
    if job.status != "running":
        raise HTTPException(
            status_code=400, 
            detail=f"Cannot cancel job because it is already {job.status}"
        )
        
    # Send the cancellation signal to the database
    job.status = "cancelled"
    db.commit()
    
    return {"status": "success", "message": f"Migration job {job_id} has been cancelled."}

@router.post("/make-public")
async def make_all_assets_public(db: Session = Depends(get_db)):
    """Adds a public playback ID to all existing signed Mux assets and updates the DB."""
    from app.database.models import Video
    videos = db.query(Video).filter(Video.mux_asset_id != None, Video.mux_playback_id != None).all()

    updated, failed = 0, 0
    results = []

    for video in videos:
        try:
            new_playback_id = await asyncio.to_thread(add_public_playback_id, video.mux_asset_id)
            video.mux_playback_id = new_playback_id
            video.mux_stream_url = f"https://stream.mux.com/{new_playback_id}.m3u8"
            db.commit()
            updated += 1
            results.append({"vimeo_id": video.vimeo_id, "status": "updated", "public_playback_id": new_playback_id})
        except Exception as e:
            failed += 1
            results.append({"vimeo_id": video.vimeo_id, "status": "failed", "error": str(e)})

    return {"updated": updated, "failed": failed, "results": results}

@router.delete("/cleanup-mux")
async def cleanup_all_mux_assets():
    """DANGER: Deletes ALL assets from the connected Mux account one by one."""
    try:
        # 1. Fetch all assets from Mux
        assets = await asyncio.to_thread(get_all_assets)
        total_assets = len(assets)
        
        if total_assets == 0:
            return {"status": "success", "message": "No assets found in Mux to delete."}

        deleted_count = 0
        failed_count = 0

        # 2. Loop through and delete them one by one
        for index, asset in enumerate(assets, start=1):
            asset_id = asset["id"]
            try:
                await asyncio.to_thread(delete_asset, asset_id)
                deleted_count += 1
            except Exception as e:
                failed_count += 1
            
            # Pause for 200ms to respect Mux API rate limits
            await asyncio.sleep(0.2)

        return {
            "status": "success",
            "message": f"Mux cleanup complete.",
            "total_found": total_assets,
            "deleted": deleted_count,
            "failed": failed_count
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/add-signed-playback")
async def backfill_signed_playback_ids(db: Session = Depends(get_db)):
    """
    One-time backfill: adds a signed playback ID to every video that doesn't have one yet.
    Run this once for videos migrated before the download feature was added.
    """
    from app.database.models import Video
    videos = db.query(Video).filter(
        Video.mux_asset_id != None,
        Video.mux_signed_playback_id == None
    ).all()

    updated, failed = 0, 0
    results = []

    for video in videos:
        try:
            signed_id = await asyncio.to_thread(add_signed_playback_id, video.mux_asset_id)
            video.mux_signed_playback_id = signed_id
            db.commit()
            updated += 1
            results.append({"vimeo_id": video.vimeo_id, "status": "updated", "signed_playback_id": signed_id})
        except Exception as e:
            failed += 1
            results.append({"vimeo_id": video.vimeo_id, "status": "failed", "error": str(e)})

    return {"updated": updated, "failed": failed, "results": results}


@router.post("/remigrate/{vimeo_id}")
async def remigrate_single_video(vimeo_id: str, db: Session = Depends(get_db)):
    """
    Re-migrates a single video: deletes the old Mux asset, clears the DB record,
    and re-processes from Vimeo. Use this for videos migrated without audio tracks.
    """
    from app.database.models import Video

    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")

    # Delete old Mux asset if it exists
    if video.mux_asset_id:
        try:
            await asyncio.to_thread(delete_asset, video.mux_asset_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete old Mux asset: {str(e)}")

    # Save what we need before clearing
    title = video.vimeo_title
    vimeo_url = video.vimeo_url
    folder_path = video.vimeo_folder_path

    # Remove old DB record so process_single_video can create a fresh one
    db.delete(video)
    db.commit()

    # Re-process: fetches from Vimeo and uploads to Mux with audio tracks enabled
    try:
        result = await asyncio.to_thread(
            process_single_video, db, title, vimeo_url, vimeo_id, folder_path
        )
        return {"status": "success", "vimeo_id": vimeo_id, "mux_result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Re-migration failed: {str(e)}")


@router.post("/attach-audio/{vimeo_id}")
async def attach_audio(vimeo_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db), language: Optional[str] = None):
    """
    Manually re-triggers audio attachment for an already-migrated video.
    Use ?language=es to attach only a specific language and avoid duplicates.
    """
    from app.database.models import Video
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")
    if not video.mux_asset_id:
        raise HTTPException(status_code=400, detail="Video has no Mux asset ID.")
    if not video.vimeo_url:
        raise HTTPException(status_code=400, detail="Video has no Vimeo URL stored.")

    background_tasks.add_task(
        attach_audio_tracks_background,
        video.mux_asset_id,
        video.vimeo_id,
        video.vimeo_url,
        language,
    )
    return {"status": "queued", "vimeo_id": vimeo_id, "mux_asset_id": video.mux_asset_id, "language": language or "all"}


@router.post("/folder-migration")
async def start_folder_migration(
    folder_url: str, 
    limit: Optional[int] = None, 
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db)
):
    # Extract folder ID from URL (e.g., .../folder/28548971 -> 28548971)
    try:
        folder_id = folder_url.split("/folder/")[-1].split("?")[0]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Vimeo folder URL")

    # Prevent concurrent migrations
    if db.query(MigrationJob).filter(MigrationJob.status == "running").first():
        raise HTTPException(status_code=400, detail="A migration is already in progress.")

    job = MigrationJob(status="running")
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_folder_migration, job.id, folder_id, limit)

    return {"status": "Folder migration started", "job_id": job.id, "folder_id": folder_id}


@router.post("/migrate-ids")
async def migrate_ids(
    vimeo_ids: List[str],
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Migrate a specific list of Vimeo IDs. Already-migrated IDs are skipped automatically.
    Pass Vimeo IDs as a JSON array: ["123456", "789012", ...]
    """
    if not vimeo_ids:
        raise HTTPException(status_code=400, detail="vimeo_ids list is empty.")

    if db.query(MigrationJob).filter(MigrationJob.status == "running").first():
        raise HTTPException(status_code=400, detail="A migration is already in progress.")

    job = MigrationJob(status="running", total_videos=len(vimeo_ids))
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(run_ids_migration, job.id, vimeo_ids)

    return {"status": "Migration started", "job_id": job.id, "requested": len(vimeo_ids)}


@router.get("/verify-folder")
async def verify_folder_migration(folder_url: str, db: Session = Depends(get_db)):
    """
    Fetches all videos from a Vimeo folder and checks which are migrated to Mux.
    Returns counts and lists of migrated, pending, and failed videos.
    """
    from app.database.models import Video
    from app.services.vimeo_service import get_vimeo_folder_videos
    import asyncio

    try:
        folder_id = folder_url.split("/folder/")[-1].split("?")[0]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Vimeo folder URL")

    try:
        all_videos = await asyncio.to_thread(get_vimeo_folder_videos, folder_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Vimeo folder: {str(e)}")

    migrated, pending, failed = [], [], []

    for item in all_videos:
        v = item["video"]
        vimeo_id = v["uri"].split("/")[-1]
        title = v.get("name", "Untitled")
        record = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()

        entry = {"vimeo_id": vimeo_id, "title": title}
        if record and record.mux_asset_id:
            entry["mux_asset_id"] = record.mux_asset_id
            entry["mux_playback_id"] = record.mux_playback_id
            entry["status"] = record.status
            migrated.append(entry)
        elif record:
            entry["status"] = record.status or "processing"
            failed.append(entry)
        else:
            pending.append(entry)

    total = len(all_videos)
    return {
        "folder_id": folder_id,
        "total_in_vimeo": total,
        "migrated_count": len(migrated),
        "pending_count": len(pending),
        "failed_count": len(failed),
        "all_migrated": len(migrated) == total,
        "migrated": migrated,
        "pending": pending,
        "failed": failed,
    }