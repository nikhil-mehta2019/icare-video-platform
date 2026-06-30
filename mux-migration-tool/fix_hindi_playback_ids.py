"""
fix_hindi_playback_ids.py
-------------------------
Fetches all playback IDs from Mux API for ALL Hindi videos where
mux_signed_playback_id is NULL, and updates the DB with the correct values
mapped by policy:
  policy="public" → mux_playback_id
  policy="signed" → mux_signed_playback_id

If no signed playback ID exists on the Mux asset yet, creates one.

Usage:
    python fix_hindi_playback_ids.py --dry-run   # preview only
    python fix_hindi_playback_ids.py             # apply fixes
"""

import os, sys, argparse
sys.path.insert(0, os.path.dirname(__file__))

import requests
from dotenv import load_dotenv
load_dotenv()

from app.database.session import SessionLocal
from app.database.models import Video

MUX_TOKEN_ID     = os.getenv("MUX_TOKEN_ID")
MUX_TOKEN_SECRET = os.getenv("MUX_TOKEN_SECRET")
AUTH             = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)
MUX_BASE         = "https://api.mux.com/video/v1"


def fetch_playback_ids(asset_id: str) -> dict:
    """Returns {"public": id, "signed": id} for whatever policies exist."""
    r = requests.get(f"{MUX_BASE}/assets/{asset_id}", auth=AUTH, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Mux API error ({r.status_code}): {r.text[:200]}")
    playbacks = r.json().get("data", {}).get("playback_ids", [])
    result = {}
    for p in playbacks:
        result[p["policy"]] = p["id"]
    return result


def create_signed_playback_id(asset_id: str) -> str:
    """Creates a signed playback ID on the Mux asset and returns the new ID."""
    r = requests.post(
        f"{MUX_BASE}/assets/{asset_id}/playback-ids",
        json={"policy": "signed"},
        auth=AUTH, timeout=15
    )
    if not r.ok:
        raise RuntimeError(f"Create signed playback ID failed ({r.status_code}): {r.text[:200]}")
    return r.json()["data"]["id"]


def main(dry_run: bool):
    with SessionLocal() as db:
        # Target: all Hindi videos missing a signed playback ID
        videos = (
            db.query(Video)
            .filter(Video.vimeo_title.ilike("%Hindi%"))
            .filter(Video.mux_asset_id.isnot(None))
            .filter(
                (Video.mux_signed_playback_id.is_(None)) |
                (Video.mux_signed_playback_id == "")
            )
            .order_by(Video.id)
            .all()
        )

        print(f"{'DRY RUN — ' if dry_run else ''}Found {len(videos)} Hindi videos missing signed playback ID\n")

        fixed = skipped = failed = 0

        for v in videos:
            print(f"[{v.id}] {v.vimeo_title}")
            print(f"  asset={v.mux_asset_id}")

            try:
                playbacks = fetch_playback_ids(v.mux_asset_id)
            except RuntimeError as e:
                print(f"  ❌ {e}")
                failed += 1
                continue

            public_id = playbacks.get("public")
            signed_id = playbacks.get("signed")

            print(f"  Mux — public={public_id or '—'} | signed={signed_id or '—'}")

            # Create signed playback ID if it doesn't exist on Mux yet
            if not signed_id and not dry_run:
                try:
                    signed_id = create_signed_playback_id(v.mux_asset_id)
                    print(f"  ✨ Created signed playback ID: {signed_id}")
                except RuntimeError as e:
                    print(f"  ❌ Could not create signed ID: {e}")
                    failed += 1
                    continue
            elif not signed_id and dry_run:
                print(f"  ✨ Would create signed playback ID")

            if not dry_run:
                if public_id:
                    v.mux_playback_id = public_id
                if signed_id:
                    v.mux_signed_playback_id = signed_id
                db.commit()
                print(f"  ✅ DB updated — signed={signed_id}")
            else:
                print(f"  ✅ Would set signed_playback_id={signed_id or '(new)'}")
            fixed += 1

        print(f"\n{'─'*60}")
        print(f"Fixed: {fixed}  |  Skipped: {skipped}  |  Failed: {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
