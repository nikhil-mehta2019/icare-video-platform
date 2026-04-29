from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import generate_playback_token, generate_download_token, generate_drm_license_token, generate_offline_license_token
from app.config import DRM_CONFIGURATION_ID, API_KEY
from app.services.migration_service import process_single_video
from pydantic import BaseModel
from typing import Optional


router = APIRouter(prefix="/videos", tags=["Videos"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: str = Security(_api_key_header)):
    if not key or key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key.")
    return key


class VimeoImportRequest(BaseModel):
    vimeo_url: str
    title: Optional[str] = "Untitled"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest, db: Session = Depends(get_db), _: str = Depends(verify_api_key)):
    try:
        parts = data.vimeo_url.rstrip("/").split("?")[0].split("/")
        vimeo_id = next(p for p in reversed(parts) if p.isdigit())
        result = process_single_video(
            db=db,
            title=data.title,
            vimeo_url=data.vimeo_url,
            vimeo_id=vimeo_id,
            folder_path="Manual Import"
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{vimeo_id}/play")
def get_secure_playback_data(
    vimeo_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()

    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")
    if not video.mux_signed_playback_id:
        raise HTTPException(status_code=400, detail="Video does not have a signed playback ID.")

    expiration_hours = 6
    secure_token = generate_playback_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
    secure_stream_url = f"https://stream.mux.com/{video.mux_signed_playback_id}.m3u8?token={secure_token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_signed_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours,
        "drm_enabled": bool(DRM_CONFIGURATION_ID),
    }

    if DRM_CONFIGURATION_ID:
        license_token = generate_drm_license_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
        response["drm_license_token"] = license_token
        response["drm_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_signed_playback_id}?token={license_token}"

    return response


@router.get("/{vimeo_id}/download")
def get_download_url(
    vimeo_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")

    download_playback_id = video.mux_drm_playback_id or video.mux_signed_playback_id
    if not download_playback_id:
        raise HTTPException(status_code=400, detail="This video does not have a signed playback ID for downloads.")

    token = generate_download_token(download_playback_id, expiration_hours=1)
    download_url = f"https://stream.mux.com/{download_playback_id}/high.mp4?token={token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title,
        "download_url": download_url,
        "token_expires_in_hours": 1,
        "drm_enabled": bool(DRM_CONFIGURATION_ID and video.mux_drm_playback_id),
    }

    if DRM_CONFIGURATION_ID and video.mux_drm_playback_id:
        offline_token = generate_offline_license_token(video.mux_drm_playback_id, expiration_hours=48)
        response["drm_offline_license_token"] = offline_token
        response["drm_widevine_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_drm_playback_id}?token={offline_token}"
        response["drm_fairplay_license_url"] = f"https://license.mux.com/license/fairplay/{video.mux_drm_playback_id}?token={offline_token}"
        response["drm_fairplay_cert_url"] = "https://license.mux.com/fairplay/cert"

    return response
