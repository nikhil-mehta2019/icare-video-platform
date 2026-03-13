from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import MigrationJob
from app.services.migration_service import run_bulk_migration
from app.services.report_service import generate_migration_excel
from app.schemas.response_models import MigrationResponse

router = APIRouter(prefix="/migration", tags=["Migration"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/vimeo-account", response_model=MigrationResponse)
def start_migration(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = MigrationJob()
    db.add(job)
    db.commit()
    db.refresh(job)

    # Trigger background job
    background_tasks.add_task(run_bulk_migration, job.id)
    
    return {"status": "Migration started", "job_id": job.id}

@router.get("/export")
def export_migration_report():
    excel_file = generate_migration_excel()
    
    return StreamingResponse(
        excel_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=vimeo_mux_mapping.xlsx"}
    )