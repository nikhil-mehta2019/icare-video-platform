import logging
from fastapi import APIRouter, Depends, HTTPException, Security, UploadFile, File
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import generate_playback_token, generate_download_token, generate_drm_license_token, generate_offline_license_token, delete_asset, list_all_assets
from app.config import DRM_CONFIGURATION_ID, API_KEY
from app.services.migration_service import process_single_video
from pydantic import BaseModel
from typing import Optional


router = APIRouter(prefix="/videos", tags=["Videos"])

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: str = Security(_api_key_header)):
    if not key or key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing API key.")
    return key


class VimeoImportRequest(BaseModel):
    vimeo_url: str
    title: Optional[str] = "Untitled"


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest, db: Session = Depends(get_db), _: str = Depends(verify_api_key)):
    try:
        parts = data.vimeo_url.rstrip("/").split("?")[0].split("/")
        vimeo_id = next(p for p in reversed(parts) if p.isdigit())
        result = process_single_video(
            db=db,
            title=data.title,
            vimeo_url=data.vimeo_url,
            vimeo_id=vimeo_id,
            folder_path="Manual Import"
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{vimeo_id}/play")
def get_secure_playback_data(
    vimeo_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()

    if not video:
        raise HTTPException(status_code=404, detail="Video not found in database.")
    if not video.mux_signed_playback_id:
        raise HTTPException(status_code=400, detail="Video does not have a signed playback ID.")

    expiration_hours = 6
    secure_token = generate_playback_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
    secure_stream_url = f"https://stream.mux.com/{video.mux_signed_playback_id}.m3u8?token={secure_token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_signed_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours,
        "drm_enabled": bool(DRM_CONFIGURATION_ID),
    }

    if DRM_CONFIGURATION_ID:
        license_token = generate_drm_license_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
        response["drm_license_token"] = license_token
        response["drm_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_signed_playback_id}?token={license_token}"

    return response


@router.get("/by-mux-id/{mux_playback_id}/play")
def get_secure_playback_data_by_mux_id(
    mux_playback_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.mux_playback_id == mux_playback_id).first()

    if not video:
        # also check signed playback id
        video = db.query(Video).filter(Video.mux_signed_playback_id == mux_playback_id).first()

    if not video:
        raise HTTPException(status_code=404, detail="Video not found for this Mux playback ID.")
    if not video.mux_signed_playback_id:
        raise HTTPException(status_code=400, detail="Video does not have a signed playback ID.")

    expiration_hours = 6
    secure_token = generate_playback_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
    secure_stream_url = f"https://stream.mux.com/{video.mux_signed_playback_id}.m3u8?token={secure_token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title or "Untitled Video",
        "playback_id": video.mux_signed_playback_id,
        "secure_stream_url": secure_stream_url,
        "playback_token": secure_token,
        "token_expires_in_hours": expiration_hours,
        "drm_enabled": bool(DRM_CONFIGURATION_ID),
    }

    if DRM_CONFIGURATION_ID:
        license_token = generate_drm_license_token(video.mux_signed_playback_id, expiration_hours=expiration_hours)
        response["drm_license_token"] = license_token
        response["drm_license_url"] = f"https://license.mux.com/license/widevine/{video.mux_signed_playback_id}?token={license_token}"

    return response


@router.get("/by-mux-id/{mux_playback_id}/download")
def get_download_url_by_mux_id(
    mux_playback_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.mux_playback_id == mux_playback_id).first()

    if not video:
        video = db.query(Video).filter(Video.mux_signed_playback_id == mux_playback_id).first()

    logger.info(
        "[DRM-DOWNLOAD-ENDPOINT]\n"
        f"  incomingPlaybackId={mux_playback_id}\n"
        f"  resolvedVideoId={video.id if video else None}\n"
        f"  mux_playback_id={video.mux_playback_id if video else None}\n"
        f"  mux_signed_playback_id={video.mux_signed_playback_id if video else None}\n"
        f"  mux_drm_playback_id={video.mux_drm_playback_id if video else None}"
    )

    # No DB record — treat the supplied playback ID as a signed playback ID directly
    if not video:
        token = generate_download_token(mux_playback_id, expiration_hours=1)
        download_url = f"https://stream.mux.com/{mux_playback_id}/high.mp4?token={token}"
        manifest_url = f"https://stream.mux.com/{mux_playback_id}.m3u8"
        response = {
            "status": "success",
            "vimeo_id": None,
            "title": None,
            "playback_id": mux_playback_id,
            "download_url": download_url,
            "token_expires_in_hours": 1,
            "drm_enabled": bool(DRM_CONFIGURATION_ID),
        }
        if DRM_CONFIGURATION_ID:
            offline_token = generate_offline_license_token(mux_playback_id, expiration_hours=48)
            response["offline"] = {
                "playback_id": mux_playback_id,
                "download_token": token,
                "drm_token": offline_token,
                "widevine_license_url": f"https://license.mux.com/license/widevine/{mux_playback_id}?token={offline_token}",
                "fairplay_license_url": f"https://license.mux.com/license/fairplay/{mux_playback_id}?token={offline_token}",
                "fairplay_cert_url": "https://license.mux.com/fairplay/cert",
            }
        logger.info(
            "[DRM-DOWNLOAD-ENDPOINT] pre-return (no DB record)\n"
            f"  offline.playback_id={mux_playback_id}\n"
            f"  drm_enabled={response['drm_enabled']}\n"
            f"  widevine_license_url={response.get('offline', {}).get('widevine_license_url')}\n"
            f"  manifestUrl={manifest_url}"
        )
        return response

    download_playback_id = video.mux_drm_playback_id or video.mux_signed_playback_id
    if not download_playback_id:
        raise HTTPException(status_code=400, detail="This video does not have a signed playback ID for downloads.")

    token = generate_download_token(download_playback_id, expiration_hours=1)
    download_url = f"https://stream.mux.com/{download_playback_id}/high.mp4?token={token}"
    manifest_url = f"https://stream.mux.com/{download_playback_id}.m3u8"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title,
        "download_url": download_url,
        "token_expires_in_hours": 1,
        "drm_enabled": bool(DRM_CONFIGURATION_ID and video.mux_drm_playback_id),
    }

    if DRM_CONFIGURATION_ID and video.mux_drm_playback_id:
        download_token = generate_download_token(video.mux_drm_playback_id, expiration_hours=6)
        offline_token = generate_offline_license_token(video.mux_drm_playback_id, expiration_hours=48)
        response["offline"] = {
            "playback_id": video.mux_drm_playback_id,
            "download_token": download_token,
            "drm_token": offline_token,
            "widevine_license_url": f"https://license.mux.com/license/widevine/{video.mux_drm_playback_id}?token={offline_token}",
            "fairplay_license_url": f"https://license.mux.com/license/fairplay/{video.mux_drm_playback_id}?token={offline_token}",
            "fairplay_cert_url": "https://license.mux.com/fairplay/cert",
        }

    logger.info(
        "[DRM-DOWNLOAD-ENDPOINT] pre-return\n"
        f"  offline.playback_id={response.get('offline', {}).get('playback_id')}\n"
        f"  drm_enabled={response['drm_enabled']}\n"
        f"  widevine_license_url={response.get('offline', {}).get('widevine_license_url')}\n"
        f"  manifestUrl={manifest_url}"
    )

    return response


class DeleteAssetsRequest(BaseModel):
    asset_ids: list[str]


def _delete_asset_ids(asset_ids: list[str], db: Session, skip_db: bool = False) -> list[dict]:
    results = []
    for asset_id in asset_ids:
        asset_id = str(asset_id).strip()
        if not asset_id:
            continue
        try:
            delete_asset(asset_id)
        except Exception as e:
            results.append({"asset_id": asset_id, "status": "failed", "error": str(e)})
            continue

        if skip_db:
            results.append({"asset_id": asset_id, "status": "deleted", "db_record": "skipped"})
            continue

        # Mux delete succeeded — remove DB record, recovering from any broken transaction
        try:
            db.rollback()
            video = db.query(Video).filter(Video.mux_asset_id == asset_id).first()
            if video:
                db.delete(video)
                db.commit()
                results.append({"asset_id": asset_id, "status": "deleted", "db_record": "removed"})
            else:
                results.append({"asset_id": asset_id, "status": "deleted", "db_record": "not_found"})
        except Exception as e:
            db.rollback()
            results.append({"asset_id": asset_id, "status": "deleted", "db_record": "error", "db_error": str(e)})
    return results


@router.delete("/assets")
def delete_mux_assets(
    data: DeleteAssetsRequest,
    skip_db: bool = False,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    return {"results": _delete_asset_ids(data.asset_ids, db, skip_db=skip_db)}


@router.delete("/assets/from-excel")
async def delete_mux_assets_from_excel(
    file: UploadFile = File(...),
    skip_db: bool = False,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    import io
    import pandas as pd

    content = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not parse Excel file: {e}")

    # Accept column named 'asset_id', 'Asset ID', 'mux_asset_id', or first column
    col = next(
        (c for c in df.columns if str(c).strip().lower().replace(" ", "_") in ("asset_id", "mux_asset_id")),
        df.columns[0]
    )
    asset_ids = df[col].dropna().astype(str).tolist()
    if not asset_ids:
        raise HTTPException(status_code=400, detail="No asset IDs found in the file.")

    return {"total": len(asset_ids), "results": _delete_asset_ids(asset_ids, db, skip_db=skip_db)}


@router.get("/{vimeo_id}/download")
def get_download_url(
    vimeo_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(verify_api_key)
):
    video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found.")

    download_playback_id = video.mux_drm_playback_id or video.mux_signed_playback_id
    if not download_playback_id:
        raise HTTPException(status_code=400, detail="This video does not have a signed playback ID for downloads.")

    token = generate_download_token(download_playback_id, expiration_hours=1)
    download_url = f"https://stream.mux.com/{download_playback_id}/high.mp4?token={token}"

    response = {
        "status": "success",
        "vimeo_id": video.vimeo_id,
        "title": video.vimeo_title,
        "download_url": download_url,
        "token_expires_in_hours": 1,
        "drm_enabled": bool(DRM_CONFIGURATION_ID and video.mux_drm_playback_id),
    }

    if DRM_CONFIGURATION_ID and video.mux_drm_playback_id:
        download_token = generate_download_token(video.mux_drm_playback_id, expiration_hours=6)
        offline_token = generate_offline_license_token(video.mux_drm_playback_id, expiration_hours=48)
        response["offline"] = {
            "playback_id": video.mux_drm_playback_id,
            "download_token": download_token,
            "drm_token": offline_token,
            "widevine_license_url": f"https://license.mux.com/license/widevine/{video.mux_drm_playback_id}?token={offline_token}",
            "fairplay_license_url": f"https://license.mux.com/license/fairplay/{video.mux_drm_playback_id}?token={offline_token}",
            "fairplay_cert_url": "https://license.mux.com/fairplay/cert",
        }

    return response


@router.get("/assets/export-excel")
def export_mux_assets_excel(_: str = Depends(verify_api_key)):
    import io
    import pandas as pd

    assets = list_all_assets()

    rows = []
    for a in assets:
        playback_ids = a.get("playback_ids", [])
        public_pb = next((p["id"] for p in playback_ids if p.get("policy") == "public"), "")
        signed_pb = next((p["id"] for p in playback_ids if p.get("policy") == "signed"), "")
        drm_pb = next((p["id"] for p in playback_ids if p.get("policy") == "drm"), "")
        rows.append({
            "asset_id": a.get("id", ""),
            "status": a.get("status", ""),
            "duration_seconds": a.get("duration", ""),
            "created_at": a.get("created_at", ""),
            "playback_id_public": public_pb,
            "playback_id_signed": signed_pb,
            "playback_id_drm": drm_pb,
            "max_stored_resolution": a.get("max_stored_resolution", ""),
            "max_stored_frame_rate": a.get("max_stored_frame_rate", ""),
            "aspect_ratio": a.get("aspect_ratio", ""),
            "passthrough": a.get("passthrough", ""),
        })

    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=mux_assets.xlsx"}
    )
