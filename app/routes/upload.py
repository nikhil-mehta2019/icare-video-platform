import os
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import SERVER_BASE_URL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["Upload"])

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, "temp_audio")


@router.post("/temp-file/{filename}", summary="Upload a file to temp storage and get a public URL")
async def upload_temp_file(filename: str, request: Request):
    """
    Accepts raw bytes (Content-Type: application/octet-stream) for any file size.
    Returns {"url": "<public URL>"} that Mux can fetch to attach the track.
    After Mux has fetched it, call DELETE /upload/temp-file/{filename} to clean up.
    """
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    dest = os.path.join(TEMP_AUDIO_DIR, filename)

    try:
        with open(dest, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    size_mb = os.path.getsize(dest) / 1_000_000
    from urllib.parse import quote
    url = f"{SERVER_BASE_URL}/temp-audio/{quote(filename)}"
    logger.info(f"[Upload] Saved {filename} ({size_mb:.2f} MB) → {url}")
    return JSONResponse({"url": url, "filename": filename, "size_mb": round(size_mb, 2)})


@router.delete("/temp-file/{filename}", summary="Delete a file from temp storage")
async def delete_temp_file(filename: str):
    path = os.path.join(TEMP_AUDIO_DIR, filename)
    if not os.path.exists(path):
        return {"deleted": False, "reason": "file not found"}
    try:
        os.remove(path)
        logger.info(f"[Upload] Deleted temp file: {filename}")
        return {"deleted": True, "filename": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
