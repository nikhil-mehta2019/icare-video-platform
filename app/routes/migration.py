import asyncio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import MigrationJob
from app.services.migration_service import run_bulk_migration
from app.services.report_service import generate_migration_excel
from app.schemas.response_models import MigrationResponse
from app.schemas.request_models import BulkMigrationRequest

router = APIRouter(prefix="/migration", tags=["Migration"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/vimeo-account", response_model=MigrationResponse)
async def start_migration(
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
    asyncio.create_task(run_bulk_migration(job.id, request.limit))
    
    return {"status": "Migration started", "job_id": job.id}

@router.get("/export")
def export_migration_report():
    excel_file = generate_migration_excel()
    
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=vimeo_mux_mapping.xlsx"}
    )

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