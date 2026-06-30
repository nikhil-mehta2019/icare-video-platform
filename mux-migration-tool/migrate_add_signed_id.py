"""
migrate_add_signed_id.py
------------------------
One-time migration:
  1. Adds mux_signed_playback_id column to the videos table (if not already present).
  2. Backfills it from mux_playback_id for all DRM assets (where mux_drm_playback_id IS NOT NULL).
     (For DRM assets, mux_playback_id was already set to the signed ID by fix_playback_ids.py.)

Run ONCE on the server before restarting the app:
    python migrate_add_signed_id.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database.session import engine
from sqlalchemy import text

def run():
    with engine.connect() as conn:
        # 1. Add column if missing
        try:
            conn.execute(text(
                "ALTER TABLE videos ADD mux_signed_playback_id VARCHAR(100) NULL"
            ))
            conn.commit()
            print("✅ Column mux_signed_playback_id added.")
        except Exception as e:
            if "already" in str(e).lower() or "duplicate" in str(e).lower() or "exist" in str(e).lower():
                print("ℹ️  Column mux_signed_playback_id already exists — skipping ALTER.")
            else:
                print(f"⚠️  ALTER TABLE warning: {e}")

        # 2. Backfill: for DRM assets where signed ID not yet saved,
        #    copy current mux_playback_id → mux_signed_playback_id
        result = conn.execute(text("""
            UPDATE videos
            SET mux_signed_playback_id = mux_playback_id
            WHERE mux_drm_playback_id IS NOT NULL
              AND mux_playback_id IS NOT NULL
              AND mux_signed_playback_id IS NULL
        """))
        conn.commit()
        print(f"✅ Backfilled mux_signed_playback_id for {result.rowcount} DRM asset(s).")

        # 3. Show summary
        rows = conn.execute(text("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN mux_signed_playback_id IS NOT NULL THEN 1 ELSE 0 END) AS has_signed,
                SUM(CASE WHEN mux_playback_id IS NOT NULL THEN 1 ELSE 0 END) AS has_public,
                SUM(CASE WHEN mux_drm_playback_id IS NOT NULL THEN 1 ELSE 0 END) AS has_drm
            FROM videos
        """)).fetchone()
        print(f"\nSummary:")
        print(f"  Total videos : {rows[0]}")
        print(f"  Has public ID: {rows[2]}")
        print(f"  Has signed ID: {rows[1]}")
        print(f"  Has DRM ID   : {rows[3]}")

if __name__ == "__main__":
    run()
