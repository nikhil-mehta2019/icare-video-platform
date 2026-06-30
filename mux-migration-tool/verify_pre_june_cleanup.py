"""
verify_pre_june_cleanup.py
--------------------------
Verifies that no pre-June 2026 videos still have Mux IDs in the DB.
Also cross-checks against live Mux API to confirm assets are actually gone.

Usage:
    python verify_pre_june_cleanup.py            # DB check only (fast)
    python verify_pre_june_cleanup.py --mux      # DB + live Mux API check (slower)
"""

import os, sys, argparse, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import requests
from app.database.session import SessionLocal
from app.database.models import Video

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"
CUTOFF           = datetime(2026, 6, 1)


def check_db():
    with SessionLocal() as db:
        # Videos before June 1 that still have a Mux asset ID
        still_have_asset = (
            db.query(Video)
            .filter(Video.created_at < CUTOFF)
            .filter(Video.mux_asset_id.isnot(None))
            .filter(Video.mux_asset_id != "")
            .order_by(Video.id)
            .all()
        )

        # Videos before June 1 that still have any Mux playback ID
        still_have_playback = (
            db.query(Video)
            .filter(Video.created_at < CUTOFF)
            .filter(
                Video.mux_playback_id.isnot(None) |
                Video.mux_signed_playback_id.isnot(None) |
                Video.mux_drm_playback_id.isnot(None)
            )
            .filter(Video.mux_asset_id.is_(None))  # asset cleared but playback IDs lingering
            .order_by(Video.id)
            .all()
        )

        total_pre_june = (
            db.query(Video)
            .filter(Video.created_at < CUTOFF)
            .count()
        )

    return still_have_asset, still_have_playback, total_pre_june


def check_mux_live(videos):
    """For each video still in DB with mux_asset_id, confirm it's actually gone from Mux."""
    still_live = []
    for v in videos:
        r = requests.get(f"{MUX_BASE}/assets/{v.mux_asset_id}", auth=AUTH, timeout=10)
        if r.status_code == 200:
            still_live.append(v)
            print(f"  ⚠️  STILL LIVE on Mux: {v.vimeo_title[:50]} | asset={v.mux_asset_id}")
        elif r.status_code == 404:
            print(f"  ✅ Confirmed deleted on Mux: {v.mux_asset_id}")
        else:
            print(f"  ❓ Mux returned {r.status_code} for {v.mux_asset_id}")
        time.sleep(0.1)
    return still_live


def main(check_mux: bool):
    print(f"Cutoff: {CUTOFF.date()} (videos created before this date)\n")

    still_have_asset, still_have_playback, total_pre_june = check_db()

    print(f"{'='*60}")
    print(f"DB Summary")
    print(f"{'='*60}")
    print(f"Total pre-June videos in DB   : {total_pre_june}")
    print(f"Still have mux_asset_id       : {len(still_have_asset)}")
    print(f"Lingering playback IDs only   : {len(still_have_playback)}")
    print()

    if not still_have_asset and not still_have_playback:
        print("✅  All pre-June Mux IDs have been cleared from the DB.")
    else:
        if still_have_asset:
            print(f"⚠️  {len(still_have_asset)} video(s) still have mux_asset_id in DB:")
            print(f"{'─'*60}")
            for v in still_have_asset:
                print(f"  db_id={v.id}")
                print(f"  title={v.vimeo_title}")
                print(f"  created_at={str(v.created_at)[:10]}")
                print(f"  mux_asset_id={v.mux_asset_id}")
                print(f"  mux_playback_id={v.mux_playback_id}")
                print(f"  mux_signed_playback_id={v.mux_signed_playback_id}")
                print(f"  status={v.status}")
                print()

        if still_have_playback:
            print(f"⚠️  {len(still_have_playback)} video(s) have lingering playback IDs (asset already cleared):")
            print(f"{'─'*60}")
            for v in still_have_playback:
                print(f"  db_id={v.id} | title={v.vimeo_title}")
                print(f"  mux_playback_id={v.mux_playback_id}")
                print(f"  mux_signed_playback_id={v.mux_signed_playback_id}")
                print(f"  mux_drm_playback_id={v.mux_drm_playback_id}")
                print()

    # Optional live Mux check
    if check_mux and still_have_asset:
        print(f"{'='*60}")
        print(f"Live Mux API check for {len(still_have_asset)} remaining asset(s)")
        print(f"{'='*60}")
        still_live = check_mux_live(still_have_asset)
        print()
        if still_live:
            print(f"❌  {len(still_live)} asset(s) still exist on Mux and need to be deleted!")
        else:
            print(f"✅  All flagged assets confirmed deleted on Mux (DB just needs cleanup).")
    elif check_mux and not still_have_asset:
        print("✅  Nothing to verify on Mux — DB is clean.")

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mux", action="store_true",
                        help="Also hit Mux API to confirm assets are actually deleted")
    args = parser.parse_args()
    main(check_mux=args.mux)
