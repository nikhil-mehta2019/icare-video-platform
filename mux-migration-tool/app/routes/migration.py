import asyncio
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from app.database.session import SessionLocal
from app.database.models import MigrationJob, Video, MigrationError
from app.services.migration_service import run_folder_migration
from app.services.report_service import generate_migration_excel
from app.services.vimeo_service import get_vimeo_folder_videos

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/migration", tags=["Migration"])


class FolderMigrationRequest(BaseModel):
    folder_url: str
    title_suffix: Optional[str] = None   # e.g. " (New)" — appended to Mux title only
    limit: Optional[int] = None


@router.post("/folder", summary="Start a Vimeo folder migration")
async def start_folder_migration(body: FolderMigrationRequest, background_tasks: BackgroundTasks):
    """
    Kicks off migration of all videos in a Vimeo folder to Mux.

    - folder_url   : e.g. https://vimeo.com/manage/folders/28548971
    - title_suffix : optional string appended to the Mux title only (students see it temporarily).
                     Use PATCH /titles/strip-suffix afterwards to remove it.
    - limit        : optional cap on number of videos to migrate in this run.

    Returns a job_id you can poll via GET /migration/status/{job_id}.
    """
    with SessionLocal() as db:
        job = MigrationJob(
            folder_url=body.folder_url,
            title_suffix=body.title_suffix,
            status="running",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

    background_tasks.add_task(
        run_folder_migration,
        job_id, body.folder_url, body.limit, body.title_suffix
    )
    return {"job_id": job_id, "status": "running", "folder_url": body.folder_url}


@router.get("/status/{job_id}", summary="Check migration job progress")
def job_status(job_id: int):
    with SessionLocal() as db:
        job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
        if not job:
            raise HTTPException(404, "Job not found")
        return {
            "job_id": job.id,
            "status": job.status,
            "total": job.total_videos,
            "imported": job.imported_videos,
            "failed": job.failed_videos,
            "folder_url": job.folder_url,
            "title_suffix": job.title_suffix,
            "started_at": job.created_at.isoformat() if job.created_at else None,
        }


@router.post("/cancel/{job_id}", summary="Cancel a running migration job")
def cancel_job(job_id: int):
    with SessionLocal() as db:
        job = db.query(MigrationJob).filter(MigrationJob.id == job_id).first()
        if not job:
            raise HTTPException(404, "Job not found")
        if job.status != "running":
            raise HTTPException(400, f"Job is '{job.status}', not running")
        job.status = "cancelled"
        db.commit()
    return {"job_id": job_id, "status": "cancelled"}


@router.get("/errors/{job_id}", summary="List failed videos for a job")
def job_errors(job_id: int):
    with SessionLocal() as db:
        errors = db.query(MigrationError).filter(MigrationError.job_id == job_id).all()
        return [{"vimeo_id": e.vimeo_id, "error": e.error_message,
                 "at": e.created_at.isoformat()} for e in errors]


@router.get("/export", summary="Download Excel report of migrated videos")
def export_excel(
    title_suffix: Optional[str] = None,
    job_id: Optional[int] = None,
    job_ids: Optional[str] = None,   # comma-separated, e.g. "49,50,51,52,53"
):
    """
    title_suffix : filter by display_title suffix, e.g. ?title_suffix= (New_Romance)
    job_id       : filter to a single job
    job_ids      : comma-separated list of job IDs, e.g. ?job_ids=49,50,51,52,53
                   (supersedes job_id when provided)
    """
    parsed_job_ids = None
    if job_ids:
        try:
            parsed_job_ids = [int(x.strip()) for x in job_ids.split(",") if x.strip()]
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(400, "job_ids must be comma-separated integers, e.g. 49,50,51")

    output = generate_migration_excel(
        title_suffix=title_suffix,
        job_id=job_id,
        job_ids=parsed_job_ids,
    )
    if parsed_job_ids:
        label = f"jobs_{'_'.join(str(j) for j in parsed_job_ids)}"
    elif job_id:
        label = f"job_{job_id}"
    elif title_suffix:
        label = title_suffix.strip().replace(" ", "_")
    else:
        label = "all"
    filename = f"migration_report_{label}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/verify-folder", summary="Cross-reference a Vimeo folder against the DB")
def verify_folder(folder_url: str):
    """
    Fetches live Vimeo folder contents and compares against the DB.
    Returns per-video status: migrated | processing | pending.
    """
    folder_id = folder_url.rstrip("/").split("/")[-1].split("?")[0]
    all_videos = get_vimeo_folder_videos(folder_id)

    with SessionLocal() as db:
        db_records = {v.vimeo_id: v.status for v in db.query(Video).all()}

    results = []
    for item in all_videos:
        vid = item["video"]
        vimeo_id = vid["uri"].split("/")[-1]
        status = db_records.get(vimeo_id, "pending")
        results.append({
            "vimeo_id": vimeo_id,
            "title": vid.get("name"),
            "folder": item["folder_name"],
            "migration_status": status,
        })

    summary = {k: sum(1 for r in results if r["migration_status"] == k)
               for k in ["migrated", "processing", "pending", "errored", "ready"]}
    return {"total": len(results), "summary": summary, "videos": results}
