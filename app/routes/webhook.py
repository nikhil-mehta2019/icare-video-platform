from fastapi import APIRouter, Request, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["Webhooks"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/mux")
async def mux_webhook(request: Request, db: Session = Depends(get_db)):
    logger.info("[Webhook] Received incoming webhook from Mux.")
    try:
        payload = await request.json()
    except Exception:
        logger.error("[Webhook] ❌ Failed to parse incoming JSON payload.")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type")
    data = payload.get("data", {})
    asset_id = data.get("id")

    logger.info(f"[Webhook] Event Type: {event_type} | Asset ID: {asset_id}")

    if not event_type or not asset_id:
        logger.warning("[Webhook] ⚠️ Webhook missing event_type or asset_id. Ignoring.")
        return {"status": "ignored", "message": "Missing type or asset ID"}

    logger.info(f"[Webhook] Looking up asset ID {asset_id} in database...")
    video = db.query(Video).filter(Video.mux_asset_id == asset_id).first()
    
    if not video:
        logger.warning(f"[Webhook] ⚠️ Asset ID {asset_id} not found in database. Ignoring.")
        return {"status": "ignored", "message": "Asset ID not found in mapping"}

    logger.info(f"[Webhook] Match found! Updating video record for Vimeo ID: {video.vimeo_id}")

    if event_type == "video.asset.ready":
        video.status = "ready"
        logger.info(f"[Webhook] Status updated to 'ready' for {video.vimeo_id}.")
        
        tracks = data.get("tracks", [])
        logger.info(f"[Webhook] Inspecting {len(tracks)} raw tracks returned by Mux...")
        
        cap_langs = []
        aud_langs = []
        
        for track in tracks:
            track_type = track.get("type")
            track_lang = track.get("language_code", "unknown")
            
            if track_type == "text" and track.get("text_type") in ["subtitles", "captions"]:
                cap_langs.append(track_lang)
                logger.info(f"[Webhook] Verified text track: {track_lang}")
            elif track_type == "audio":
                aud_langs.append(track_lang)
                logger.info(f"[Webhook] Verified audio track: {track_lang}")
                
        video.captions_count = len(cap_langs)
        video.captions_languages = ", ".join(cap_langs) if cap_langs else None
        
        video.audio_tracks_count = len(aud_langs)
        video.audio_languages = ", ".join(aud_langs) if aud_langs else None
        
        logger.info(f"[Webhook] Committing verified track counts to database...")
        db.commit()
        logger.info(f"[Webhook] ✅ Final Verification: Video {video.vimeo_id} saved with {video.captions_count} captions & {video.audio_tracks_count} audio tracks.")
        
    elif event_type == "video.asset.errored":
        video.status = "errored"
        db.commit()
        logger.error(f"[Webhook] ❌ Mux reported a processing error for Video {video.vimeo_id}.")

    else:
        logger.info(f"[Webhook] Unhandled event type '{event_type}'. No action taken.")

    return {"status": "success"}