import asyncio
import logging
from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.audio_service import attach_audio_tracks_background

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhooks"])

# Events we never act on — return immediately without opening a DB connection.
# This prevents pool exhaustion during high-volume webhook floods (e.g. 160 videos
# triggering track.created / track.errored / asset.updated in rapid succession).
IGNORED_EVENTS = {
    "video.asset.track.created",
    "video.asset.track.errored",
    "video.asset.track.ready",
    "video.asset.updated",
    "video.asset.deleted",
    "video.upload.asset_created",
    "video.upload.cancelled",
    "video.upload.created",
}


@router.post("/mux")
async def mux_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type")
    data = payload.get("data", {})
    asset_id = data.get("id") or payload.get("object", {}).get("id")

    logger.info(f"[Webhook] {event_type} | Asset: {asset_id}")

    if not event_type or not asset_id:
        return {"status": "ignored", "reason": "missing_fields"}

    # Fast-path: skip DB entirely for events we don't handle
    if event_type in IGNORED_EVENTS:
        return {"status": "ignored", "reason": "unhandled_event"}

    # Only open a DB connection for events we actually care about:
    # video.asset.created, video.asset.ready, video.asset.errored
    db = SessionLocal()
    try:
        # Retry lookup to handle race condition where webhook fires before our DB commit
        video = None
        for attempt in range(3):
            video = db.query(Video).filter(Video.mux_asset_id == asset_id).first()
            if video:
                break
            if attempt < 2:
                logger.info(f"[Webhook] Asset {asset_id} not found — retry {attempt + 1}/3")
                await asyncio.sleep(2)

        if not video:
            logger.warning(f"[Webhook] Asset {asset_id} not found after 3 retries — not our asset, ignoring.")
            return {"status": "ignored", "reason": "asset_not_found"}

        # Extract public playback_id from payload
        playback_ids = data.get("playback_ids", [])
        playback_id = next((p["id"] for p in playback_ids if p.get("policy") == "public"), None)
        if not playback_id and playback_ids:
            playback_id = playback_ids[0].get("id")

        if event_type == "video.asset.created":
            if video.status == "processing":
                return {"status": "ignored", "reason": "already_processed"}
            video.status = "processing"
            if playback_id and not video.mux_playback_id:
                video.mux_playback_id = playback_id
                video.mux_stream_url = f"https://stream.mux.com/{playback_id}.m3u8"

        elif event_type == "video.asset.ready":
            if video.status == "ready":
                return {"status": "ignored", "reason": "already_processed"}
            video.status = "ready"
            if playback_id:
                video.mux_playback_id = playback_id
                video.mux_stream_url = f"https://stream.mux.com/{playback_id}.m3u8"

            cap_langs, aud_langs = [], []
            for track in data.get("tracks", []):
                lang = track.get("language_code", "unknown")
                if track.get("type") == "text" and track.get("text_type") in ["subtitles", "captions"]:
                    cap_langs.append(lang)
                elif track.get("type") == "audio":
                    aud_langs.append(lang)

            video.captions_count = len(cap_langs)
            video.captions_languages = ", ".join(cap_langs) if cap_langs else None
            video.audio_tracks_count = len(aud_langs)
            video.audio_languages = ", ".join(aud_langs) if aud_langs else None
            logger.info(f"[Webhook] Tracks — captions: {cap_langs or 'none'} | audio: {aud_langs or 'none'}")

            # Strip suffix slug — vimeo_id in DB may be suffixed, yt-dlp needs raw ID
            raw_vimeo_id = video.vimeo_id.split("_")[0] if "_" in video.vimeo_id else video.vimeo_id

            # Trigger background audio download + attachment via yt-dlp
            background_tasks.add_task(
                attach_audio_tracks_background,
                video.mux_asset_id,
                raw_vimeo_id,
                video.vimeo_url  # Full URL with privacy hash — needed for yt-dlp auth
            )
            logger.info(f"[Webhook] Background audio attachment queued for {video.vimeo_id}.")

        elif event_type == "video.asset.errored":
            if video.status == "errored":
                return {"status": "ignored", "reason": "already_processed"}
            video.status = "errored"
            logger.error(f"[Webhook] Mux processing error for {video.vimeo_id}")

        else:
            return {"status": "ignored", "reason": "unhandled_event"}

        db.commit()
        logger.info(f"[Webhook] ✅ {video.vimeo_id} → '{video.status}'")
        return {"status": "success", "vimeo_id": video.vimeo_id, "new_status": video.status}

    finally:
        db.close()
