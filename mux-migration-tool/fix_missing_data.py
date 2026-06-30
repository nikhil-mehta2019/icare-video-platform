"""
fix_missing_data.py
-------------------
One-shot repair script for two common post-migration issues:

  1. Missing mux_signed_playback_id
     DRM assets that got a 429 when adding the signed playback ID during migration.
     → Calls Mux API to add the signed ID, updates DB.

  2. Missing / incomplete audio tracks
     Videos where audio_tracks_count is 0 or NULL (or below a threshold you set).
     → Re-runs the full audio attachment flow for each affected video.

Usage (run from the mux-migration-tool directory with the venv active):

    # Fix everything
    python fix_missing_data.py

    # Dry run — just print what would be fixed, no changes
    python fix_missing_data.py --dry-run

    # Fix only signed IDs
    python fix_missing_data.py --signed-only

    # Fix only audio, and only for videos whose suffix slug matches
    python fix_missing_data.py --audio-only --suffix "(New_Romance)"

    # Only fix videos with fewer than N audio tracks (default: 1 = any with 0)
    python fix_missing_data.py --audio-only --min-audio-tracks 6
"""

import sys
import os
import time
import argparse
import logging

# ── Make sure the app package is importable ──────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from app.database.session import SessionLocal
from app.database.models import Video
from app.config import MUX_TOKEN_ID, MUX_TOKEN_SECRET

import requests

BASE_URL = "https://api.mux.com/video/v1"
AUTH = (MUX_TOKEN_ID, MUX_TOKEN_SECRET)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("fix_missing_data.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Rate-limit-aware request helper (same as mux_service) ────────────────────

def _mux_request(method: str, url: str, max_retries: int = 6, **kwargs) -> requests.Response:
    kwargs.setdefault("auth", AUTH)
    wait = 10
    for attempt in range(1, max_retries + 1):
        r = requests.request(method, url, **kwargs)
        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", wait))
            log.warning(f"429 on {method} {url} (attempt {attempt}/{max_retries}) — waiting {retry_after}s")
            if attempt == max_retries:
                return r
            time.sleep(retry_after)
            wait = min(wait * 2, 60)
            continue
        if r.status_code >= 500:
            log.warning(f"{r.status_code} on {method} {url} (attempt {attempt}/{max_retries}) — waiting {wait}s")
            if attempt == max_retries:
                return r
            time.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        return r
    return r


# ── Fix 1: Missing signed playback IDs ───────────────────────────────────────

def fix_signed_playback_ids(dry_run: bool = False, suffix_filter: str = None):
    """
    Finds DRM assets where mux_signed_playback_id is NULL and adds it.
    """
    with SessionLocal() as db:
        q = db.query(Video).filter(
            Video.mux_drm_playback_id.isnot(None),
            Video.mux_signed_playback_id.is_(None),
            Video.mux_asset_id.isnot(None),
        )
        if suffix_filter:
            q = q.filter(Video.display_title.like(f"%{suffix_filter}"))
        videos = q.all()

    log.info(f"[SignedID] Found {len(videos)} videos missing signed playback ID.")
    if not videos:
        return

    fixed = failed = 0
    for v in videos:
        log.info(f"[SignedID] {'[DRY RUN] ' if dry_run else ''}Processing: {v.vimeo_id} | asset={v.mux_asset_id}")
        if dry_run:
            fixed += 1
            continue

        r = _mux_request(
            "POST",
            f"{BASE_URL}/assets/{v.mux_asset_id}/playback-ids",
            json={"policy": "signed"},
        )
        if r.ok:
            signed_id = r.json()["data"]["id"]
            with SessionLocal() as db:
                vid = db.query(Video).filter(Video.id == v.id).first()
                if vid:
                    vid.mux_signed_playback_id = signed_id
                    db.commit()
            log.info(f"[SignedID] ✅ {v.vimeo_id} → signed_id={signed_id}")
            fixed += 1
        else:
            log.error(f"[SignedID] ❌ {v.vimeo_id}: {r.status_code} {r.text[:200]}")
            failed += 1

        time.sleep(0.3)  # gentle pacing between calls

    log.info(f"[SignedID] Done. Fixed={fixed}, Failed={failed}")


# ── Fix 2: Missing / incomplete audio tracks ─────────────────────────────────

def fix_audio_tracks(dry_run: bool = False, suffix_filter: str = None,
                     min_audio_tracks: int = 2, match_captions: bool = False):
    """
    Finds videos with incomplete audio tracks and re-runs the audio attachment flow.

    match_captions=True  : per-video check — audio_tracks_count < captions_count
                           (mirrors: SELECT * FROM videos WHERE audio_tracks_count < captions_count)
    match_captions=False : fixed threshold — audio_tracks_count < min_audio_tracks
    """
    import asyncio
    from app.services.audio_service import attach_audio_tracks_background
    from sqlalchemy import or_

    with SessionLocal() as db:
        q = db.query(Video).filter(
            Video.mux_asset_id.isnot(None),
            Video.status == "ready",
        )
        if suffix_filter:
            q = q.filter(Video.display_title.like(f"%{suffix_filter}"))

        if match_captions:
            # Per-video: audio_tracks_count < captions_count (exact match to your SQL query)
            q = q.filter(
                Video.captions_count.isnot(None),
                Video.captions_count > 0,
                or_(
                    Video.audio_tracks_count.is_(None),
                    Video.audio_tracks_count < Video.captions_count,
                )
            )
            mode_label = "audio_tracks_count < captions_count"
        else:
            q = q.filter(
                or_(
                    Video.audio_tracks_count.is_(None),
                    Video.audio_tracks_count < min_audio_tracks,
                )
            )
            mode_label = f"< {min_audio_tracks} audio track(s)"

        videos = q.all()

    log.info(f"[Audio] Found {len(videos)} videos where {mode_label}.")
    if not videos:
        return

    fixed = failed = skipped = 0
    for v in videos:
        # Strip suffix slug from vimeo_id to get raw Vimeo ID for API calls
        raw_vimeo_id = v.vimeo_id.split("_")[0] if "_" in v.vimeo_id else v.vimeo_id
        vimeo_url = v.vimeo_url or f"https://vimeo.com/{raw_vimeo_id}"
        log.info(
            f"[Audio] {'[DRY RUN] ' if dry_run else ''}Processing: "
            f"{v.vimeo_id} (raw={raw_vimeo_id}) | asset={v.mux_asset_id} | "
            f"current_tracks={v.audio_tracks_count}"
        )
        if dry_run:
            fixed += 1
            continue

        try:
            attached = asyncio.run(attach_audio_tracks_background(
                mux_asset_id=v.mux_asset_id,
                vimeo_id=raw_vimeo_id,
                vimeo_url=vimeo_url,
            ))
            if attached:
                log.info(f"[Audio] ✅ {v.vimeo_id} — attached {len(attached)} track(s): {attached}")
                fixed += 1
            else:
                log.info(f"[Audio] ⚠️  {v.vimeo_id} — no tracks found/attached (may have none on Vimeo)")
                skipped += 1
        except Exception as e:
            log.error(f"[Audio] ❌ {v.vimeo_id}: {e}")
            failed += 1

        # Pause between videos to avoid hammering both Vimeo and Mux APIs
        time.sleep(1)

    log.info(f"[Audio] Done. Fixed={fixed}, Skipped={skipped}, Failed={failed}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Repair missing signed IDs and audio tracks.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be done without making any changes.")
    parser.add_argument("--signed-only", action="store_true",
                        help="Only fix missing mux_signed_playback_id.")
    parser.add_argument("--audio-only", action="store_true",
                        help="Only fix missing/incomplete audio tracks.")
    parser.add_argument("--suffix", type=str, default=None,
                        help="Filter to videos whose display_title ends with this suffix, "
                             "e.g. --suffix '(New_Romance)'")
    parser.add_argument("--min-audio-tracks", type=int, default=1,
                        help="Treat videos with fewer than this many audio tracks as needing repair. "
                             "Default=1 (only repairs 0 / NULL). Set to 7 to catch partial attachments. "
                             "Ignored when --match-captions is set.")
    parser.add_argument("--match-captions", action="store_true",
                        help="Per-video mode: fix videos where audio_tracks_count < captions_count. "
                             "Mirrors: SELECT * FROM videos WHERE audio_tracks_count < captions_count")
    args = parser.parse_args()

    if args.dry_run:
        log.info("=== DRY RUN — no changes will be made ===")

    run_signed = not args.audio_only
    run_audio = not args.signed_only

    if run_signed:
        log.info("── Fixing missing signed playback IDs ──")
        fix_signed_playback_ids(dry_run=args.dry_run, suffix_filter=args.suffix)

    if run_audio:
        log.info("── Fixing missing / incomplete audio tracks ──")
        fix_audio_tracks(
            dry_run=args.dry_run,
            suffix_filter=args.suffix,
            min_audio_tracks=args.min_audio_tracks,
            match_captions=args.match_captions,
        )

    log.info("All done.")


if __name__ == "__main__":
    main()
