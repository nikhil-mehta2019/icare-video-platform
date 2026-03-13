from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video

router = APIRouter(prefix="/playback", tags=["Playback"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/{vimeo_id}")
def get_playback_info(vimeo_id: str, db: Session = Depends(get_db)):
    # Look up the Mux details based strictly on the Vimeo ID mapped in the database
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()

    if not video:
        raise HTTPException(status_code=404, detail="Video mapping not found")

    if not video.mux_playback_id:
        raise HTTPException(status_code=400, detail="Video has no Mux playback ID yet")

    return {
        "vimeo_id": video.vimeo_id,
        "mux_playback_id": video.mux_playback_id,
        "mux_stream_url": video.mux_stream_url
    }