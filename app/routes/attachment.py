import os
import io
import uuid
import logging
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.services.attachment_service import (
    _find_match,
    _normalize,
    attach_tracks_to_asset,
    read_excel_rows,
    TEMP_AUDIO_DIR,
)
from app.services.track_attachment_service import run_track_attachment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attachment", tags=["Bulk Attachment"])

# In-memory progress store keyed by job_id
_attach_jobs: dict[str, dict] = {}

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".opus", ".webm", ".ogg"}
SRT_EXTENSIONS   = {".srt", ".vtt"}


def _extract_zip_to_map(zip_bytes: bytes, allowed_extensions: set[str]) -> dict[str, str]:
    """Extract zip in-memory, write matching files to TEMP_AUDIO_DIR, return normalized→path map."""
    file_map = {}
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            fname = os.path.basename(member.filename)
            if not fname:
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in allowed_extensions:
                continue
            dest = os.path.join(TEMP_AUDIO_DIR, fname)
            with zf.open(member) as src, open(dest, "wb") as out:
                out.write(src.read())
            file_map[_normalize(fname)] = dest
    return file_map


@router.post("/attach", summary="Bulk-attach audio and/or SRT files to existing Mux assets via Excel mapping")
async def bulk_attach(
    excel_file: UploadFile = File(..., description="Excel (.xlsx) with asset_id and video_name columns"),
    audio_zip: Optional[UploadFile] = File(None, description="Zip archive of audio files (.mp3 / .m4a / .aac)"),
    srt_zip: Optional[UploadFile] = File(None, description="Zip archive of SRT/VTT subtitle files"),
    asset_id_column: str = Form("mux_asset_id", description="Column name for Mux asset ID"),
    video_name_column: str = Form("video_name", description="Column name for video title used for filename matching"),
    audio_language: str = Form("hi", description="Language code for audio tracks (e.g. hi, es, sw)"),
    audio_name: str = Form("Hindi", description="Display name for the audio track in Mux"),
    srt_language: str = Form("hi", description="Language code for subtitle tracks"),
):
    if not audio_zip and not srt_zip:
        raise HTTPException(status_code=400, detail="Provide at least one of audio_zip or srt_zip.")

    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    excel_path = os.path.join(TEMP_AUDIO_DIR, excel_file.filename)
    extracted_files: list[str] = []

    try:
        # Save Excel
        content = await excel_file.read()
        with open(excel_path, "wb") as f:
            f.write(content)

        # Extract zips into TEMP_AUDIO_DIR and build file maps
        audio_map: dict[str, str] = {}
        srt_map:   dict[str, str] = {}

        if audio_zip:
            audio_bytes = await audio_zip.read()
            try:
                audio_map = await run_in_threadpool(_extract_zip_to_map, audio_bytes, AUDIO_EXTENSIONS)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="audio_zip is not a valid zip file.")
            extracted_files.extend(audio_map.values())
            if not audio_map:
                raise HTTPException(status_code=400, detail="No audio files found inside audio_zip.")
            logger.info(f"[Attachment] Extracted {len(audio_map)} audio file(s) from zip.")

        if srt_zip:
            srt_bytes = await srt_zip.read()
            try:
                srt_map = await run_in_threadpool(_extract_zip_to_map, srt_bytes, SRT_EXTENSIONS)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="srt_zip is not a valid zip file.")
            extracted_files.extend(srt_map.values())
            if not srt_map:
                raise HTTPException(status_code=400, detail="No SRT/VTT files found inside srt_zip.")
            logger.info(f"[Attachment] Extracted {len(srt_map)} SRT file(s) from zip.")

        # Read Excel rows
        try:
            rows = await run_in_threadpool(read_excel_rows, excel_path, asset_id_column, video_name_column)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to read Excel: {e}")

        if not rows:
            raise HTTPException(status_code=400, detail="No valid rows found in Excel (check column names).")

        results = []
        for row in rows:
            asset_id   = row["asset_id"]
            video_name = row["video_name"]

            matched_audio = _find_match(video_name, audio_map) if audio_map else None
            matched_srt   = _find_match(video_name, srt_map)   if srt_map   else None

            if not matched_audio and not matched_srt:
                results.append({
                    "asset_id": asset_id,
                    "video_name": video_name,
                    "status": "skipped",
                    "reason": "no matching audio or SRT file found",
                })
                continue

            outcome = await run_in_threadpool(
                attach_tracks_to_asset,
                asset_id,
                matched_audio,
                matched_srt,
                audio_language,
                audio_name,
                srt_language,
            )

            status = "error" if outcome["errors"] and not outcome["audio"] and not outcome["srt"] else "success"
            results.append({
                "asset_id": asset_id,
                "video_name": video_name,
                "matched_audio": os.path.basename(matched_audio) if matched_audio else None,
                "matched_srt": os.path.basename(matched_srt) if matched_srt else None,
                "audio": outcome["audio"],
                "srt": outcome["srt"],
                "errors": outcome["errors"],
                "status": status,
            })

        success = sum(1 for r in results if r["status"] == "success")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors  = sum(1 for r in results if r["status"] == "error")

        return {
            "processed": len(rows),
            "success": success,
            "skipped": skipped,
            "errors": errors,
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Attachment] Unexpected error during bulk attach")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up Excel
        try:
            if os.path.exists(excel_path):
                os.remove(excel_path)
        except Exception:
            pass
        # Clean up any extracted files not already deleted by attach_tracks_to_asset
        for path in extracted_files:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass


# ── Generic track attachment from local folders ───────────────────────────────

class AttachTracksRequest(BaseModel := __import__("pydantic").BaseModel):
    srt_dir: Optional[str] = None
    audio_dir: Optional[str] = None
    lang_code: str = "sw"
    lang_name: str = "Swahili"
    cutoff_date: Optional[str] = None          # ISO date string e.g. "2026-06-01"
    manual_map: dict[str, int] = {}            # {lowercase_title: db_video_id}
    dry_run: bool = False


@router.post("/attach-tracks", summary="Attach SRT and/or audio tracks to Mux assets matched from local folders")
async def attach_tracks(req: AttachTracksRequest, background_tasks: BackgroundTasks):
    if not req.srt_dir and not req.audio_dir:
        raise HTTPException(status_code=400, detail="Provide at least one of srt_dir or audio_dir.")

    if req.srt_dir and not os.path.isdir(req.srt_dir):
        raise HTTPException(status_code=400, detail=f"srt_dir not found: {req.srt_dir}")
    if req.audio_dir and not os.path.isdir(req.audio_dir):
        raise HTTPException(status_code=400, detail=f"audio_dir not found: {req.audio_dir}")

    cutoff = None
    if req.cutoff_date:
        try:
            cutoff = datetime.fromisoformat(req.cutoff_date)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid cutoff_date: {req.cutoff_date}. Use ISO format e.g. 2026-06-01")

    job_id = str(uuid.uuid4())[:8]
    progress = {}
    _attach_jobs[job_id] = progress

    background_tasks.add_task(
        run_track_attachment,
        job_id=job_id,
        srt_dir=req.srt_dir,
        audio_dir=req.audio_dir,
        lang_code=req.lang_code,
        lang_name=req.lang_name,
        cutoff=cutoff,
        manual_map=req.manual_map,
        dry_run=req.dry_run,
        progress=progress,
    )

    return {
        "job_id": job_id,
        "message": "Track attachment started in background. Poll /attachment/attach-tracks/status/{job_id} for progress.",
        "dry_run": req.dry_run,
    }


@router.get("/attach-tracks/status/{job_id}", summary="Poll progress of a track attachment job")
def attach_tracks_status(job_id: str):
    if job_id not in _attach_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")
    return {"job_id": job_id, **_attach_jobs[job_id]}
