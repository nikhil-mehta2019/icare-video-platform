from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video, UserCourseAccess, VideoProgress
from app.services.mux_service import generate_playback_token, generate_download_token, generate_drm_license_token, generate_offline_license_token
from app.config import DRM_CONFIGURATION_ID
from app.services.migration_service import process_single_video
from app.auth import get_current_user
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


router = APIRouter(prefix="/videos", tags=["Videos"])

class VimeoImportRequest(BaseModel):
    vimeo_url: str
    title: Optional[str] = "Untitled"

class ProgressUpdate(BaseModel):
    current_time: int
    total_duration: int
    device_type: Optional[str] = "web"
    session_id: Optional[str] = None

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest, db: Session = Depends(get_db)):
    try:
        # Extract Vimeo ID — handles both:
        # https://vimeo.com/123456789
        # https://vimeo.com/123456789/privacyhash
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
    course_id: int = 1, 
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Generates a secure offline JWT token IF the user is within their 90-day window.
    Injects resume_from_seconds for cross-platform progress tracking.
    """
    # --- 1. THE 90-DAY SECURITY CHECK ---
    access = db.query(UserCourseAccess).filter(
        UserCourseAccess.user_id == user_id,
        UserCourseAccess.course_id == course_id
    ).first()

    if not access:
        raise HTTPException(status_code=403, detail="User does not have access to this training program.")
    
    if datetime.utcnow() > access.access_end:
        raise HTTPException(status_code=403, detail="Access expired. Your 90-day training window has closed.")
    
    # --- 2. THE VIDEO CHECK ---
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    
    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")
    if not video.mux_playback_id:
        raise HTTPException(status_code=400, detail="Video is still processing or failed in Mux.")

    # --- 3. PROGRESS FETCH (NEW) ---
    progress = db.query(VideoProgress).filter(
        VideoProgress.user_id == user_id,
        VideoProgress.vimeo_id == vimeo_id
    ).first()

    resume_seconds = 0
    is_completed = False
    
    if progress:
        is_completed = progress.is_completed
        # If completed, start from beginning. Otherwise, resume from last watched.
        resume_seconds = 0 if is_completed else progress.last_watched_seconds

    # --- 4. GENERATE THE SECURE TICKET ---
    expiration_hours = 6
    secure_token = generate_playback_token(video.mux_playback_id, expiration_hours=expiration_hours)
    secure_stream_url = f"https://stream.mux.com/{video.mux_playback_id}.m3u8?token={secure_token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours,
        "resume_from_seconds": resume_seconds,
        "is_completed": is_completed,
        "drm_enabled": bool(DRM_CONFIGURATION_ID),
    }

    # DRM: provide a separate license token and license URL for the player's key request
    if DRM_CONFIGURATION_ID:
        license_token = generate_drm_license_token(video.mux_playback_id, expiration_hours=expiration_hours)
        response["drm_license_token"] = license_token
        response["drm_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_playback_id}?token={license_token}"

    return response

@router.get("/{vimeo_id}/download")
def get_download_url(
    vimeo_id: str,
    course_id: int = 1,
    user_id: int = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns a short-lived signed MP4 download URL for the mobile app ONLY.
    The signed playback ID is never exposed publicly — only our backend can issue these tokens.
    """
    # 1. Access check (same 90-day gate as streaming)
    access = db.query(UserCourseAccess).filter(
        UserCourseAccess.user_id == user_id,
        UserCourseAccess.course_id == course_id
    ).first()

    if not access:
        raise HTTPException(status_code=403, detail="User does not have access to this training program.")

    if datetime.utcnow() > access.access_end:
        raise HTTPException(status_code=403, detail="Access expired. Your 90-day training window has closed.")

    # 2. Video check
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")

    # Prefer DRM playback ID if available, fall back to signed
    download_playback_id = video.mux_drm_playback_id or video.mux_signed_playback_id
    if not download_playback_id:
        raise HTTPException(status_code=400, detail="This video does not have a signed playback ID for downloads. Run the backfill endpoint first.")

    # 3. Generate download token and URL
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

    # DRM offline: provide a persistent license token the mobile app uses to fetch
    # a Widevine (Android) / FairPlay (iOS) offline license before going offline.
    if DRM_CONFIGURATION_ID and video.mux_drm_playback_id:
        offline_token = generate_offline_license_token(video.mux_drm_playback_id, expiration_hours=48)
        response["drm_offline_license_token"] = offline_token
        response["drm_widevine_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_drm_playback_id}?token={offline_token}"
        response["drm_fairplay_license_url"] = f"https://license.mux.com/license/fairplay/{video.mux_drm_playback_id}?token={offline_token}"
        response["drm_fairplay_cert_url"] = "https://license.mux.com/fairplay/cert"

    return response


# --- NEW: Progress Heartbeat API ---
@router.post("/{vimeo_id}/progress")
def update_video_progress(
    vimeo_id: str, 
    payload: ProgressUpdate, 
    user_id: int = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """
    Called every 15s by the frontend. Uses an Upsert pattern to minimize DB locking.
    """
    # Verify video exists
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")

    progress = db.query(VideoProgress).filter(
        VideoProgress.user_id == user_id,
        VideoProgress.vimeo_id == vimeo_id
    ).first()

    # Completion Logic: 95% threshold
    completion_threshold = payload.total_duration * 0.95
    reached_end = payload.current_time >= completion_threshold

    if not progress:
        # INSERT
        progress = VideoProgress(
            user_id=user_id,
            vimeo_id=vimeo_id,
            last_watched_seconds=payload.current_time,
            is_completed=reached_end,
            completed_at=datetime.utcnow() if reached_end else None,
            device_type=payload.device_type,
            session_id=payload.session_id
        )
        db.add(progress)
    else:
        # UPDATE: Prevent rapid seeking from overwriting max progress
        if payload.current_time > progress.last_watched_seconds or reached_end:
            progress.last_watched_seconds = max(progress.last_watched_seconds, payload.current_time)
            progress.device_type = payload.device_type
            progress.session_id = payload.session_id
            
            # If they hit the 95% mark for the first time
            if reached_end and not progress.is_completed:
                progress.is_completed = True
                progress.completed_at = datetime.utcnow()
                progress.last_watched_seconds = payload.total_duration # Snap to end

    db.commit()
    return {
        "status": "success", 
        "recorded_seconds": progress.last_watched_seconds, 
        "is_completed": progress.is_completed
    }