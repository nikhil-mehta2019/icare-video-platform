import os
import glob
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.manual_upload_service import run_manual_upload
from app.config import SERVER_BASE_URL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["Manual Upload"])

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_DIR = os.path.join(BASE_DIR, "temp")


@router.post("/temp-file", summary="Upload a file to the server's temp folder for Mux to fetch")
async def upload_temp_file(file: UploadFile = File(...)):
    """
    Saves an uploaded file to the server's /temp directory and returns its public URL.
    Used by batch_hindi_upload.py Phase 2 to host SRT/audio files so Mux can pull them.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    dest = os.path.join(TEMP_DIR, file.filename)
    contents = await file.read()
    with open(dest, "wb") as f:
        f.write(contents)
    url = f"{SERVER_BASE_URL}/temp/{file.filename}"
    logger.info(f"[TempUpload] Saved {file.filename} ({len(contents):,} bytes) → {url}")
    return {"url": url, "filename": file.filename, "size": len(contents)}


@router.post("/temp-raw/{filename}", summary="Upload a file as raw bytes (no multipart) — use for files > 10 MB")
async def upload_temp_raw(filename: str, request: Request):
    """
    Accepts raw bytes in the request body (Content-Type: application/octet-stream).
    Bypasses python-multipart size limits that affect the /temp-file endpoint for large files.
    Returns the same response shape as /temp-file.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)
    dest = os.path.join(TEMP_DIR, filename)
    size = 0
    with open(dest, "wb") as out:
        async for chunk in request.stream():
            out.write(chunk)
            size += len(chunk)
    url = f"{SERVER_BASE_URL}/temp/{filename}"
    logger.info(f"[TempUpload/raw] Saved {filename} ({size:,} bytes) → {url}")
    return {"url": url, "filename": filename, "size": size}


@router.delete("/temp-file/{filename}", summary="Delete a file from the server's temp folder")
async def delete_temp_file(filename: str):
    """
    Deletes a previously uploaded temp file. Called by scripts after Mux has fetched the file.
    """
    dest = os.path.join(TEMP_DIR, filename)
    if not os.path.exists(dest):
        raise HTTPException(status_code=404, detail=f"File not found: {filename}")
    os.remove(dest)
    logger.info(f"[TempUpload] Deleted temp file: {filename}")
    return {"deleted": filename}


@router.delete("/temp-file-all", summary="Delete all files from the server's temp folder")
async def delete_all_temp_files():
    """
    Clears the entire temp directory. Use to reclaim disk space after a batch run.
    """
    files = glob.glob(os.path.join(TEMP_DIR, "*"))
    deleted = []
    errors = []
    for f in files:
        try:
            os.remove(f)
            deleted.append(os.path.basename(f))
        except Exception as e:
            errors.append({"file": os.path.basename(f), "error": str(e)})
    logger.info(f"[TempUpload] Cleared temp dir: {len(deleted)} deleted, {len(errors)} errors")
    return {"deleted_count": len(deleted), "errors": errors}


class ManualUploadRequest(BaseModel):
    title: str
    video_path: str                     # Absolute local path to the video file
    srt_path: Optional[str] = None     # Absolute local path to the SRT subtitle file
    srt_language: Optional[str] = "hi"
    audio_path: Optional[str] = None   # Absolute local path to the audio-only file
    audio_language: Optional[str] = "hi"
    audio_name: Optional[str] = "Hindi"


@router.post("/manual", summary="Upload a local video file to Mux")
async def manual_upload(body: ManualUploadRequest, background_tasks: BackgroundTasks):
    """
    Uploads a video from the local filesystem to Mux with DRM.
    Intended for Hindi (or any other) videos that are not on Vimeo.

    Provide absolute paths to the video file, and optionally an SRT subtitle
    file and/or an alternate audio track file.

    The upload runs synchronously (it streams the file to Mux and waits for
    the asset_id). Track attachment happens in the background once Mux
    signals ready via webhook.

    Returns: {db_id, mux_asset_id, status: "processing", title}
    """
    try:
        result = await run_manual_upload(
            title=body.title,
            video_path=body.video_path,
            srt_path=body.srt_path,
            srt_language=body.srt_language,
            audio_path=body.audio_path,
            audio_language=body.audio_language,
            audio_name=body.audio_name,
        )
        return result
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"[Manual Upload] Failed: {e}")
        raise HTTPException(500, f"Upload failed: {e}")


@router.get("/status/{db_id}", summary="Check status of a manually uploaded video")
def upload_status(db_id: str):
    with SessionLocal() as db:
        video = db.query(Video).filter(Video.vimeo_id == db_id).first()
        if not video:
            raise HTTPException(404, "Upload record not found")
        return {
            "db_id": video.vimeo_id,
            "title": video.vimeo_title,
            "mux_asset_id": video.mux_asset_id,
            "mux_playback_id": video.mux_playback_id,
            "mux_drm_playback_id": video.mux_drm_playback_id,
            "status": video.status,
            "captions": video.captions_languages,
            "audio_tracks": video.audio_languages,
        }


@router.get("/list", summary="List all manually uploaded videos")
def list_manual_uploads():
    with SessionLocal() as db:
        videos = db.query(Video).filter(Video.source == "manual").order_by(Video.created_at.desc()).all()
        return [
            {
                "db_id": v.vimeo_id,
                "title": v.vimeo_title,
                "mux_asset_id": v.mux_asset_id,
                "status": v.status,
                "captions": v.captions_languages,
                "audio_tracks": v.audio_languages,
                "uploaded_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in videos
        ]
