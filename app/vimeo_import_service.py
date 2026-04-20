from app.services.mux_service import upload_video
from app.database.db import SessionLocal
from app.database.models import Video


def import_vimeo_video(title, video_url, course_id, order, vimeo_id):

    db = SessionLocal()

    try:

        # check if video already migrated
        existing = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()

        if existing:
            return {
                "status": "skipped",
                "message": "video already imported"
            }

        mux_data = upload_video(video_url)

        video = Video(
            title=title,
            course_id=course_id,
            mux_asset_id=mux_data["asset_id"],
            playback_id=mux_data["playback_id"],
            vimeo_id=vimeo_id,
            order=order
        )

        db.add(video)
        db.commit()

        return {
            "status": "video imported",
            "asset_id": mux_data["asset_id"],
            "playback_id": mux_data["playback_id"]
        }

    finally:
        db.close()