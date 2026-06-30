from app.database.session import SessionLocal
from app.database.models import Video
from datetime import date, datetime

today = datetime.combine(date.today(), datetime.min.time())

with SessionLocal() as db:
    videos = db.query(Video).filter(
        Video.mux_asset_id.isnot(None),
        Video.created_at >= today,
    ).order_by(Video.id.desc()).all()

    print(f"Videos migrated today ({date.today()}): {len(videos)}")
    for v in videos:
        print(f"  id={v.id} | audio={v.audio_tracks_count} | display_title={v.display_title} | vimeo_title={v.vimeo_title}")
