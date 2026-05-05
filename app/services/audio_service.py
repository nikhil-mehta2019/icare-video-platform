import os
import sys
import json
import asyncio
import subprocess
import logging
from app.config import VIMEO_ACCESS_TOKEN, SERVER_BASE_URL
from app.services.mux_service import add_audio_track

logger = logging.getLogger(__name__)

BASE_DIR       = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMP_AUDIO_DIR = os.path.join(BASE_DIR, "temp_audio")
CACHE_DIR      = os.path.join(TEMP_AUDIO_DIR, "yt_cache")
CLEANUP_DELAY_SECONDS = 60

YT_DLP_BASE = [
    sys.executable, "-m", "yt_dlp",
    "--cache-dir", CACHE_DIR,
    "--add-header", f"Authorization: Bearer {VIMEO_ACCESS_TOKEN}",
    "--no-playlist",
]


def _discover_audio_languages(vimeo_url: str) -> list[dict]:
    """
    Calls yt-dlp --dump-json on the Vimeo URL to discover available audio tracks.
    Returns a list of dicts: [{language, name}, ...]
    No HLS parsing — yt-dlp handles Vimeo format discovery natively.
    """
    cmd = YT_DLP_BASE + ["--dump-json", vimeo_url]
    logger.info(f"[Audio Service] Discovering audio languages via yt-dlp for {vimeo_url}...")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.warning(f"[Audio Service] yt-dlp format discovery failed:\n{result.stderr[-300:]}")
            return []

        info = json.loads(result.stdout)
        formats = info.get("formats", [])

        seen = set()
        tracks = []
        for f in formats:
            lang = f.get("language")
            # Only audio-only formats with a real language code
            if f.get("vcodec") == "none" and f.get("acodec") != "none" and lang and lang not in seen:
                seen.add(lang)
                tracks.append({"language": lang, "name": f.get("format_note") or lang})

        logger.info(f"[Audio Service] Discovered {len(tracks)} audio language(s): {[t['language'] for t in tracks]}")
        return tracks

    except Exception as e:
        logger.error(f"[Audio Service] Error during audio discovery: {e}")
        return []


def _download_audio(vimeo_url: str, vimeo_id: str, language: str) -> str | None:
    """
    Downloads a single audio language from Vimeo using yt-dlp.
    Returns the local file path on success, None on failure.
    """
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    output_template = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.%(ext)s")

    cmd = YT_DLP_BASE + [
        # Prefer m4a audio, fallback to any audio — no ffmpeg post-processing needed
        "-f", f"bestaudio[ext=m4a][language={language}]/bestaudio[ext=m4a]/bestaudio[language={language}]/bestaudio",
        "--no-post-overwrites",
        "-o", output_template,
        vimeo_url,
    ]

    logger.info(f"[Audio Service] Downloading '{language}' audio from Vimeo...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.warning(f"[Audio Service] yt-dlp download failed ({language}):\n{result.stderr[-500:]}")
            return None
    except subprocess.TimeoutExpired:
        logger.error(f"[Audio Service] yt-dlp timed out for {vimeo_id} ({language})")
        return None

    for ext in ["m4a", "mp3", "aac", "opus", "webm", "ogg"]:
        path = os.path.join(TEMP_AUDIO_DIR, f"{vimeo_id}_{language}.{ext}")
        if os.path.exists(path):
            logger.info(f"[Audio Service] ✅ Downloaded: {path}")
            return path

    logger.warning(f"[Audio Service] Output file not found after yt-dlp for {vimeo_id} ({language})")
    return None


async def attach_audio_tracks_background(mux_asset_id: str, vimeo_id: str, vimeo_url: str, only_language: str | None = None) -> list[str]:
    """
    Background task triggered by video.asset.ready webhook.

    Returns a list of language codes that were successfully attached to Mux.

    Flow:
      1. Call Vimeo URL via yt-dlp to discover available audio languages
      2. Download each language audio track via yt-dlp
      3. Serve it locally via FastAPI /temp-audio
      4. Attach to the existing Mux asset
      5. Delete temp file after 60s (Mux fetches within a few minutes)
    """
    logger.info(f"[Audio Service] Starting audio attachment for Vimeo {vimeo_id} → Mux {mux_asset_id}")
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Step 1: Discover audio languages from same Vimeo URL
    audio_tracks = await asyncio.to_thread(_discover_audio_languages, vimeo_url)
    if not audio_tracks:
        logger.info(f"[Audio Service] No alternate audio tracks found for {vimeo_id}. Nothing to attach.")
        return []

    # Step 2-5: Download, serve, attach, cleanup — one track at a time
    if only_language:
        audio_tracks = [t for t in audio_tracks if t["language"] == only_language]
        if not audio_tracks:
            logger.warning(f"[Audio Service] Language '{only_language}' not found in Vimeo tracks. Nothing to attach.")
            return []

    attached_languages = []

    for track in audio_tracks:
        language = track["language"]
        name = track["name"]
        file_path = None

        try:
            file_path = await asyncio.to_thread(_download_audio, vimeo_url, vimeo_id, language)
            if not file_path:
                logger.warning(f"[Audio Service] Skipping '{name}' ({language}) — download failed.")
                continue

            filename = os.path.basename(file_path)
            public_url = f"{SERVER_BASE_URL}/temp-audio/{filename}"
            logger.info(f"[Audio Service] Serving at: {public_url}")

            await asyncio.to_thread(add_audio_track, mux_asset_id, public_url, language, name)
            logger.info(f"[Audio Service] ✅ '{name}' ({language}) attached to Mux asset.")
            attached_languages.append(language)

            logger.info(f"[Audio Service] Waiting {CLEANUP_DELAY_SECONDS}s before deleting temp file...")
            await asyncio.sleep(CLEANUP_DELAY_SECONDS)

        except Exception as e:
            logger.error(f"[Audio Service] ❌ Failed for '{name}' ({language}): {e}")
        finally:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"[Audio Service] Deleted temp file: {file_path}")

    return attached_languages
