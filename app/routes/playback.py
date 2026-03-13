from fastapi import APIRouter, HTTPException, Query
from app.database.db import SessionLocal
from app.database.models import Video
from app.services.access_service import check_access

router = APIRouter(prefix="/playback", tags=["Playback"])


@router.get("/{video_id}")
def play_video(video_id: int, email: str = Query(...)):

    db = SessionLocal()

    try:

        video = db.query(Video).filter(Video.id == video_id).first()

        if not video:
            raise HTTPException(status_code=404, detail="Video not found")

        course_id = video.course_id

        allowed = check_access(email, course_id)

        if not allowed:
            raise HTTPException(status_code=403, detail="Access expired")

        return {
            "playback_id": video.playback_id
        }

    finally:
        db.close()