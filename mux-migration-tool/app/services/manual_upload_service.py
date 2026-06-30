"""
Manual upload service — for Hindi (or any) videos not on Vimeo.

Flow:
  1. Caller provides: local video path, title, optional SRT path, optional audio path + language.
  2. A Mux Direct Upload slot is created (with DRM if configured).
  3. The local file is streamed directly to Mux (no intermediate copy).
  4. We poll for the asset_id from the upload record.
  5. A DB record is saved immediately with status="processing".
  6. Once Mux fires video.asset.ready (via webhook), the record is updated to "ready".
  7. If SRT or audio paths were provided, they are served via /temp/<filename>
     and attached to the Mux asset after it becomes ready.

Notes:
  - vimeo_id in DB is set to  "manual_<slug>"  — unique per upload.
  - source is set to  "manual"  so it's easy to filter.
  - Audio attachment for manual uploads is handled synchronously in a background task
    (called directly from the webhook handler, same as Vimeo videos).
    Since the audio file is already local, it skips yt-dlp discovery.
"""

import os
import re
import asyncio
import logging
from datetime import datetime

from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import (
    create_direct_upload,
    push_file_to_upload_url,
    poll_upload_for_asset_id,
    add_audio_track,
    add_text_track,
)
from app.config import SERVER_BASE_URL

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_DIR = os.path.join(BASE_DIR, "temp")


def _slug(title: str) -> str:
    """Creates a filesystem-safe slug from a title."""
    slug = re.sub(r"[^\w\s-]", "", title.lower()).strip()
    return re.sub(r"[\s_-]+", "_", slug)[:60]


def _unique_db_id(title: str) -> str:
    """Generates a unique vimeo_id for a manually uploaded video."""
    ts = datetime.utcnow().strftime("%y%m%d%H%M%S")
    return f"manual_{_slug(title)}_{ts}"


async def run_manual_upload(
    title: str,
    video_path: str,
    srt_path: str = None,
    srt_language: str = "hi",
    audio_path: str = None,
    audio_language: str = "hi",
    audio_name: str = "Hindi",
) -> dict:
    """
    Uploads a local video file to Mux.
    Attaches SRT and audio track if paths are provided.
    Returns the saved Video record as a dict.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError(f"Video file not found: {video_path}")
    if srt_path and not os.path.isfile(srt_path):
        raise FileNotFoundError(f"SRT file not found: {srt_path}")
    if audio_path and not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    logger.info(f"[Manual Upload] Starting: '{title}' from {video_path}")

    # Step 1: Create Mux Direct Upload slot
    upload_info = await asyncio.to_thread(create_direct_upload, title)
    upload_id = upload_info["upload_id"]
    upload_url = upload_info["upload_url"]
    logger.info(f"[Manual Upload] Upload slot created: {upload_id}")

    # Step 2: Stream file to Mux
    logger.info(f"[Manual Upload] Uploading {os.path.getsize(video_path):,} bytes to Mux...")
    await asyncio.to_thread(push_file_to_upload_url, upload_url, video_path)
    logger.info(f"[Manual Upload] File upload complete.")

    # Step 3: Poll for asset_id
    asset_id = await asyncio.to_thread(poll_upload_for_asset_id, upload_id)
    logger.info(f"[Manual Upload] Asset ID: {asset_id}")

    # Step 4: Save DB record immediately
    db_id = _unique_db_id(title)
    with SessionLocal() as db:
        video = Video(
            vimeo_id=db_id,
            vimeo_title=title,
            display_title=title,
            vimeo_url="",
            vimeo_folder_path="Manual Upload",
            source="manual",
            mux_asset_id=asset_id,
            mux_playback_id=None,       # filled by webhook
            mux_drm_playback_id=None,   # filled by webhook
            mux_stream_url=None,
            status="processing",
        )

        # Copy SRT and audio to temp folder so they can be served to Mux
        if srt_path:
            srt_filename = f"{db_id}_{srt_language}.srt"
            dest = os.path.join(TEMP_DIR, srt_filename)
            os.makedirs(TEMP_DIR, exist_ok=True)
            import shutil
            shutil.copy2(srt_path, dest)
            video.captions_count = 1
            video.captions_languages = srt_language
            # Store temp file info in passthrough for webhook to pick up
            video.vimeo_url = f"srt:{srt_filename}:{srt_language}"  # re-used field as temp signal

        if audio_path:
            audio_ext = os.path.splitext(audio_path)[1]
            audio_filename = f"{db_id}_{audio_language}{audio_ext}"
            dest = os.path.join(TEMP_DIR, audio_filename)
            os.makedirs(TEMP_DIR, exist_ok=True)
            import shutil
            shutil.copy2(audio_path, dest)
            video.audio_tracks_count = 1
            video.audio_languages = audio_language

        db.add(video)
        db.commit()
        db.refresh(video)
        result = {
            "db_id": video.vimeo_id,
            "mux_asset_id": asset_id,
            "status": "processing",
            "title": title,
        }

    logger.info(f"[Manual Upload] ✅ DB record saved. Waiting for Mux webhook to confirm ready.")
    return result


async def attach_manual_tracks(mux_asset_id: str, db_id: str,
                               srt_language: str = None, audio_language: str = None,
                               audio_name: str = "Hindi"):
    """
    Called from the webhook handler when a manually-uploaded asset becomes ready.
    Attaches SRT and audio tracks from temp files that were saved during upload.
    """
    os.makedirs(TEMP_DIR, exist_ok=True)

    # Attach SRT if present
    if srt_language:
        for ext in ["srt", "vtt"]:
            srt_path = os.path.join(TEMP_DIR, f"{db_id}_{srt_language}.{ext}")
            if os.path.exists(srt_path):
                filename = os.path.basename(srt_path)
                url = f"{SERVER_BASE_URL}/temp/{filename}"
                try:
                    await asyncio.to_thread(
                        add_text_track, mux_asset_id, url, srt_language, srt_language
                    )
                    logger.info(f"[Manual Upload] ✅ SRT ({srt_language}) attached to {mux_asset_id}")
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.error(f"[Manual Upload] ❌ SRT attach failed: {e}")
                finally:
                    if os.path.exists(srt_path):
                        os.remove(srt_path)
                break

    # Attach audio if present
    if audio_language:
        for ext in ["m4a", "mp4", "mp3", "aac"]:
            audio_path = os.path.join(TEMP_DIR, f"{db_id}_{audio_language}.{ext}")
            if os.path.exists(audio_path):
                filename = os.path.basename(audio_path)
                url = f"{SERVER_BASE_URL}/temp/{filename}"
                try:
                    await asyncio.to_thread(
                        add_audio_track, mux_asset_id, url, audio_language, audio_name
                    )
                    logger.info(f"[Manual Upload] ✅ Audio ({audio_language}) attached to {mux_asset_id}")
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.error(f"[Manual Upload] ❌ Audio attach failed: {e}")
                finally:
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                break
