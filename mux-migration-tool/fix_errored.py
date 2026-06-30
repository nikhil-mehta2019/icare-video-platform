"""
fix_errored.py
--------------
For videos where Mux asset actually errored (non-ready on Mux side):
  1. Saves video info from DB
  2. Deletes the bad DB record
  3. Re-uploads to Mux via process_single_video
  4. Audio attaches automatically via webhook when Mux finishes processing
"""
import sys, time
sys.path.insert(0, '.')
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.migration_service import process_single_video

vimeo_ids = ['1081925556_(New_Romance)', '1088271208_(New_Slavic)']

with SessionLocal() as db:
    for vid_id in vimeo_ids:
        v = db.query(Video).filter(Video.vimeo_id == vid_id).first()
        if not v:
            print(f'NOT FOUND: {vid_id}')
            continue

        # Save info before deleting
        raw_vimeo_id  = v.vimeo_id.split('_')[0]
        vimeo_url     = v.vimeo_url
        vimeo_title   = v.vimeo_title
        folder_path   = v.vimeo_folder_path

        # Extract original suffix from vimeo_id (e.g. "1081925556_(New_Romance)" → " (New_Romance)")
        parts = v.vimeo_id.split('_', 1)
        suffix_slug = parts[1] if len(parts) > 1 else None
        title_suffix = f" {suffix_slug}" if suffix_slug else None  # e.g. " (New_Romance)"

        print(f'\nDeleting errored record: {v.vimeo_id}')
        db.delete(v)
        db.commit()

        print(f'Re-migrating: {raw_vimeo_id} with suffix "{title_suffix}"')
        try:
            result = process_single_video(
                db=db,
                title=vimeo_title,
                vimeo_url=vimeo_url,
                vimeo_id=raw_vimeo_id,
                folder_path=folder_path,
                title_suffix=title_suffix,
            )
            print(f'  Result: {result}')
        except Exception as e:
            print(f'  ERROR: {e}')

        time.sleep(2)

print('\nDone. Monitor server logs — audio will attach automatically via webhook.')
