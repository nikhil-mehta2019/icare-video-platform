import asyncio
import logging
from fastapi import APIRouter, Request, BackgroundTasks
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.audio_service import attach_audio_tracks_background

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhook"])

# Events we never act on — return immediately without opening a DB connection.
# This prevents pool exhaustion during high-volume webhook floods
# (e.g. 100+ videos each triggering track.created / asset.updated in rapid succession).
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
    """
    Handles Mux asset lifecycle events.
    - video.asset.created  → mark processing
    - video.asset.ready    → mark ready, sync tracks, enqueue audio attachment
    - video.asset.errored  → mark errored
    """
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "reason": "invalid_json"}

    event_type = payload.get("type")
    data = payload.get("data", {})
    asset_id = data.get("id")

    logger.info(f"[Webhook] {event_type} | asset: {asset_id}")

    if not event_type or not asset_id:
        return {"status": "ignored", "reason": "missing_fields"}

    # Fast-path: skip DB entirely for events we don't handle
    if event_type in IGNORED_EVENTS:
        return {"status": "ignored", "reason": "unhandled_event"}

    # Retry to handle the race condition where the webhook fires before our DB commit
    video = None
    with SessionLocal() as db:
        for attempt in range(3):
            video = db.query(Video).filter(Video.mux_asset_id == asset_id).first()
            if video:
                break
            if attempt < 2:
                await asyncio.sleep(2)

        if not video:
            logger.info(f"[Webhook] Asset {asset_id} not in DB — not our asset, ignoring.")
            return {"status": "ignored", "reason": "asset_not_found"}

        playback_ids = data.get("playback_ids", [])
        drm_id = next((p["id"] for p in playback_ids if p.get("policy") == "drm"), None)
        signed_id = next((p["id"] for p in playback_ids if p.get("policy") == "signed"), None)
        public_id = next((p["id"] for p in playback_ids if p.get("policy") == "public"), None)
        first_id = playback_ids[0]["id"] if playback_ids else None

        if event_type == "video.asset.created":
            if video.status != "processing":
                video.status = "processing"

        elif event_type == "video.asset.ready":
            video.status = "ready"

            # Update playback IDs
            if drm_id:
                video.mux_drm_playback_id = drm_id
            if signed_id:
                video.mux_signed_playback_id = signed_id
            if public_id:
                video.mux_playback_id = public_id
                video.mux_stream_url = f"https://stream.mux.com/{public_id}.m3u8"
            elif first_id and not video.mux_playback_id:
                video.mux_playback_id = first_id
                video.mux_stream_url = f"https://stream.mux.com/{first_id}.m3u8"

            # Sync track counts from payload
            cap_langs, aud_langs = [], []
            for track in data.get("tracks", []):
                lang = track.get("language_code", "unknown")
                if track.get("type") == "text":
                    cap_langs.append(lang)
                elif track.get("type") == "audio":
                    aud_langs.append(lang)
            video.captions_count = len(cap_langs)
            video.captions_languages = ", ".join(cap_langs) if cap_langs else video.captions_languages
            video.audio_tracks_count = len(aud_langs)
            video.audio_languages = ", ".join(aud_langs) if aud_langs else video.audio_languages

            # Enqueue audio attachment (different for Vimeo vs manual uploads)
            vimeo_url = video.vimeo_url
            raw_vimeo_id = video.vimeo_id
            source = video.source or "vimeo"
            db_id = video.vimeo_id

            db.commit()

            if source == "vimeo" and vimeo_url and not vimeo_url.startswith("srt:"):
                # Vimeo: use yt-dlp to download and attach alternate audio
                raw_id = raw_vimeo_id.split("_")[0] if "_" in raw_vimeo_id else raw_vimeo_id
                background_tasks.add_task(
                    attach_audio_tracks_background,
                    asset_id, raw_id, vimeo_url
                )
                logger.info(f"[Webhook] Audio attachment queued for Vimeo {raw_id}")

            elif source == "manual":
                # Manual: attach pre-copied SRT/audio from temp folder
                from app.services.manual_upload_service import attach_manual_tracks
                audio_lang = video.audio_languages
                srt_lang = video.captions_languages
                background_tasks.add_task(
                    attach_manual_tracks,
                    asset_id, db_id,
                    srt_lang, audio_lang
                )
                logger.info(f"[Webhook] Manual track attachment queued for {db_id}")

            return {"status": "success", "event": event_type, "db_id": raw_vimeo_id}

        elif event_type == "video.asset.errored":
            video.status = "errored"
            logger.error(f"[Webhook] Mux reported error for asset {asset_id}")

        else:
            return {"status": "ignored", "reason": "unhandled_event"}

        db.commit()

    return {"status": "success", "event": event_type}
