"""
fix_public_ids.py
-----------------
For the 160 (New) DRM assets that currently have NO public playback ID:
  1. Adds a public playback policy to the Mux asset (for Mux dashboard preview)
  2. Moves existing mux_playback_id (signed) → mux_signed_playback_id in DB
  3. Saves the new public ID → mux_playback_id in DB

Run AFTER migrate_add_signed_id.py:
    python fix_public_ids.py --dry-run          # preview only
    python fix_public_ids.py                    # apply fixes
    python fix_public_ids.py --title-suffix " (New)"   # only (New) videos
"""

import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET
from app.database.session import SessionLocal
from app.database.models import Video

BASE_URL = "https://api.mux.com/video/v1"
AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)

def get_assets(title_suffix=None):
    from sqlalchemy import text
    suffix_clause = "AND display_title LIKE :suffix" if title_suffix else ""
    sql = f"""
        SELECT id, vimeo_id, mux_asset_id, mux_playback_id,
               mux_signed_playback_id, display_title, vimeo_title
        FROM videos
        WHERE mux_asset_id IS NOT NULL
          AND mux_drm_playback_id IS NOT NULL
          AND mux_signed_playback_id IS NOT NULL
          AND mux_playback_id = mux_signed_playback_id
          {suffix_clause}
    """
    params = {"suffix": f"%{title_suffix}"} if title_suffix else {}
    with SessionLocal() as db:
        rows = db.execute(text(sql), params).fetchall()
    return [
        {
            "id": r[0],
            "vimeo_id": r[1],
            "mux_asset_id": r[2],
            "mux_playback_id": r[3],
            "mux_signed_playback_id": r[4],
            "title": r[5] or r[6],
        }
        for r in rows
    ]

def add_public_playback_id(asset_id, attempt=1):
    r = requests.post(
        f"{BASE_URL}/assets/{asset_id}/playback-ids",
        json={"policy": "public"},
        auth=AUTH,
    )
    if r.status_code == 429 and attempt <= 5:
        wait = 10 * attempt
        print(f"    ⏳ Rate limited — waiting {wait}s...")
        time.sleep(wait)
        return add_public_playback_id(asset_id, attempt + 1)
    if not r.ok:
        raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["data"]["id"]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--title-suffix", type=str, default=None)
    args = parser.parse_args()

    assets = get_assets(args.title_suffix)
    print(f"Found {len(assets)} asset(s) needing public playback ID.\n")

    if not assets:
        return

    if args.dry_run:
        for a in assets:
            print(f"  {a['mux_asset_id']}  {a['title']}")
        return

    fixed = failed = 0
    for i, a in enumerate(assets, 1):
        print(f"[{i}/{len(assets)}] {a['mux_asset_id']} — {a['title']}")
        try:
            public_id = add_public_playback_id(a["mux_asset_id"])
            with SessionLocal() as db:
                from sqlalchemy import text
                db.execute(text("""
                    UPDATE videos
                    SET mux_playback_id = :pub,
                        mux_stream_url  = :url
                    WHERE mux_asset_id = :aid
                """), {
                    "pub": public_id,
                    "url": f"https://stream.mux.com/{public_id}.m3u8",
                    "aid": a["mux_asset_id"],
                })
                db.commit()
            print(f"  ✅ Public ID: {public_id}")
            fixed += 1
        except Exception as e:
            print(f"  ❌ {e}")
            failed += 1

    print(f"\nDone. Fixed: {fixed} | Failed: {failed}")

if __name__ == "__main__":
    main()
