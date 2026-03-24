from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video, UserCourseAccess, VideoProgress
from app.services.mux_service import generate_playback_token
from app.services.migration_service import process_single_video
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


router = APIRouter(prefix="/videos", tags=["Videos"])

class VimeoImportRequest(BaseModel):
    vimeo_url: str
    title: Optional[str] = "Untitled"

# --- NEW: Schema for Progress Payload ---
class ProgressUpdate(BaseModel):
    current_time: int
    total_duration: int  # Required to calculate the 95% completion threshold
    device_type: Optional[str] = "web"
    session_id: Optional[str] = None

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- NEW: Simulated Auth Dependency ---
def get_current_user(x_user_id: int = Header(..., description="Simulated Auth Token/Header")):
    """Extracts user ID from Auth headers. Replace with real JWT decoding later."""
    return x_user_id

@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest, db: Session = Depends(get_db)):
    try:
        # Automatically extract Vimeo ID, stripping trailing slashes and query parameters
        vimeo_id = data.vimeo_url.rstrip("/").split("/")[-1].split("?")[0]
        
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
    user_id: int = Depends(get_current_user), # <-- CHANGED to Header Auth
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

    return {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours,
        "resume_from_seconds": resume_seconds, # NEW
        "is_completed": is_completed           # NEW
    }

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