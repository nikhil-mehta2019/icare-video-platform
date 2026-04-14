import os
import sys
import asyncio
import subprocess
import logging
from app.config import VIMEO_ACCESS_TOKEN, SERVER_BASE_URL
from app.services.vimeo_service import get_video_audio_tracks
from app.services.mux_service import add_audio_track

logger = logging.getLogger(__name__)

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, "temp_audio")
# Mux typically fetches within a few minutes — 10 min is a safe buffer before deleting
CLEANUP_DELAY_SECONDS = 600


def _download_audio(vimeo_id: str, language: str) -> str | None:
    """
    Uses yt-dlp to download a specific language audio track from Vimeo.
    Returns the local file path on success, None on failure.
    """
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    output_template = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.%(ext)s")

    cmd = [
        sys.executable, "-m", "yt_dlp",   # Use current Python env — avoids Windows PATH issues
        "--add-header", f"Authorization: Bearer {VIMEO_ACCESS_TOKEN}",
        "-f", f"bestaudio[language={language}]/bestaudio",
        "--extract-audio",
        "--audio-format", "m4a",
        "--no-playlist",
        "-o", output_template,
        f"https://vimeo.com/{vimeo_id}"
    ]

    logger.info(f"[Audio Service] Downloading '{language}' audio for Vimeo ID {vimeo_id}...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.warning(f"[Audio Service] yt-dlp failed for {vimeo_id} ({language}): {result.stderr[-300:]}")
            return None
    except subprocess.TimeoutExpired:
        logger.error(f"[Audio Service] yt-dlp timed out for {vimeo_id} ({language})")
        return None

    # Find the downloaded file (ext may vary before conversion)
    for ext in ["m4a", "mp3", "aac", "opus", "webm", "ogg"]:
        path = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.{ext}")
        if os.path.exists(path):
            logger.info(f"[Audio Service] ✅ Downloaded: {path}")
            return path

    logger.warning(f"[Audio Service] File not found after yt-dlp completed for {vimeo_id} ({language})")
    return None


async def attach_audio_tracks_background(mux_asset_id: str, vimeo_id: str):
    """
    Background task triggered by video.asset.ready webhook.
    Downloads each alternate audio track via yt-dlp, serves it temporarily,
    attaches to the Mux asset, then deletes the local file.
    """
    logger.info(f"[Audio Service] Starting background audio attachment for Vimeo {vimeo_id} → Mux {mux_asset_id}")

    audio_tracks = get_video_audio_tracks(vimeo_id)
    if not audio_tracks:
        logger.info(f"[Audio Service] No alternate audio tracks found for {vimeo_id}.")
        return

    logger.info(f"[Audio Service] Found {len(audio_tracks)} alternate audio track(s) to attach.")

    for track in audio_tracks:
        language = track.get("language") or "en"
        name = track.get("name") or language
        file_path = None

        try:
            # Step 1: Download audio via yt-dlp
            file_path = await asyncio.to_thread(_download_audio, vimeo_id, language)
            if not file_path:
                logger.warning(f"[Audio Service] Skipping '{name}' ({language}) — download failed.")
                continue

            # Step 2: Build public URL served by our FastAPI static mount
            filename = os.path.basename(file_path)
            public_url = f"{SERVER_BASE_URL}/temp-audio/{filename}"
            logger.info(f"[Audio Service] Serving at: {public_url}")

            # Step 3: Attach to Mux asset
            await asyncio.to_thread(add_audio_track, mux_asset_id, public_url, language, name)
            logger.info(f"[Audio Service] ✅ Audio track '{name}' ({language}) attached to Mux asset.")

            # Step 4: Wait before deleting — gives Mux time to fetch the file
            logger.info(f"[Audio Service] Waiting {CLEANUP_DELAY_SECONDS}s before deleting temp file...")
            await asyncio.sleep(CLEANUP_DELAY_SECONDS)

        except Exception as e:
            logger.error(f"[Audio Service] ❌ Failed to attach audio track '{name}' ({language}): {e}")
        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"[Audio Service] Deleted temp file: {file_path}")
