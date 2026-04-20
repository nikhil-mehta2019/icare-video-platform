from app.database.models import MigrationJob, Video
from app.database.db import SessionLocal

from app.services.vimeo_service import get_vimeo_videos, get_video_download_url
from app.services.vimeo_import_service import import_vimeo_video


def migrate_vimeo_account(course_id):

    videos = get_vimeo_videos()

    db = SessionLocal()

    job = MigrationJob(
        course_id=course_id,
        total_videos=len(videos)
    )

    db.add(job)
    db.commit()
    db.refresh(job)

    order_counter = 1

    for v in videos:

        try:

            video_id = v["uri"].split("/")[-1]

            existing = db.query(Video).filter(Video.vimeo_id == video_id).first()

            if existing:
                print(f"Skipping already migrated video: {v['name']}")
                continue

            video_url = get_video_download_url(v["uri"])

            import_vimeo_video(
                v["name"],
                video_url,
                course_id,
                order_counter,
                video_id
            )

            job.imported_videos += 1
            order_counter += 1

        except Exception as e:
            job.failed_videos += 1
            print(f"Failed to import {v['name']}: {str(e)}")

        db.commit()

    job.status = "completed"
    db.commit()

    return {"migration_id": job.id}