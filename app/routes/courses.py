from fastapi import APIRouter
from app.database.db import SessionLocal
from app.database.models import Video

router = APIRouter(prefix="/courses", tags=["Courses"])


@router.get("/{course_id}/videos")
def get_course_videos(course_id: int):

    db = SessionLocal()

    try:

        videos = db.query(Video).filter(Video.course_id == course_id).order_by(Video.order).all()

        result = []

        for v in videos:
            result.append({
                "video_id": v.id,
                "title": v.title,
                "order": v.order
            })

        return result

    finally:
        db.close()