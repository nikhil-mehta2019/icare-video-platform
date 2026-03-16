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
    """Receives asynchronous webhooks from Mux to update video status."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type")
    data = payload.get("data", {})
    asset_id = data.get("id")

    if not event_type or not asset_id:
        return {"status": "ignored", "message": "Missing type or asset ID"}

    # Find the video in our database by the Mux Asset ID
    video = db.query(Video).filter(Video.mux_asset_id == asset_id).first()
    if not video:
        logger.warning(f"Received webhook for unknown asset_id: {asset_id}")
        return {"status": "ignored", "message": "Asset ID not found in mapping"}

    # Handle status updates
    if event_type == "video.asset.ready":
        video.status = "ready"
        db.commit()
        logger.info(f"Video {video.vimeo_id} marked as ready via Mux webhook.")
        
    elif event_type == "video.asset.errored":
        video.status = "errored"
        db.commit()
        logger.error(f"Video {video.vimeo_id} failed processing inside Mux.")

    return {"status": "success"}