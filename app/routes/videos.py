from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import generate_playback_token
from app.services.migration_service import process_single_video
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/videos", tags=["Videos"])

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
def get_secure_playback_data(vimeo_id: str, db: Session = Depends(get_db)):
    """
    Provides secure playback data for a specific video.
    Used by both the Wix Web Player and the Base44 Mobile App.
    """
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    
    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")
    
    if not video.mux_playback_id:
        raise HTTPException(status_code=400, detail="Video is still processing or failed in Mux.")

    # Generate a token valid for 6 hours
    expiration_hours = 6
    secure_token = generate_playback_token(video.mux_playback_id, expiration_hours=expiration_hours)

    # Construct the full secure HLS URL for the mobile app
    secure_stream_url = f"https://stream.mux.com/{video.mux_playback_id}.m3u8?token={secure_token}"

    return {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours
    }