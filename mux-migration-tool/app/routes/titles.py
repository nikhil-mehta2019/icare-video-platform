import logging
from fastapi import APIRouter, HTTPException
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import update_asset_title

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/titles", tags=["Title Updates"])


@router.patch("/strip-suffix", summary="Remove suffix from Mux titles and update DB")
def strip_suffix(suffix: str, dry_run: bool = False):
    """
    Strips `suffix` from the end of every Mux asset title where it appears,
    and PATCHes the Mux asset meta.title to the clean version.

    Call this after confirming all newly migrated assets are ready and
    you no longer need the suffix visible to students.

    - suffix  : the string to strip, e.g. " (New)"
    - dry_run : preview what would change without making API calls.

    Example: PATCH /titles/strip-suffix?suffix=%20(New)
    """
    with SessionLocal() as db:
        affected = db.query(Video).filter(Video.display_title.contains(suffix)).all()

        if not affected:
            return {"message": f"No assets found with suffix '{suffix}'", "updated": 0}

        if dry_run:
            return {
                "dry_run": True,
                "would_update": len(affected),
                "preview": [
                    {"vimeo_id": v.vimeo_id,
                     "current_title": v.display_title,
                     "new_title": v.display_title.replace(suffix, "").strip()}
                    for v in affected
                ],
            }

        updated, failed = [], []
        for v in affected:
            clean_title = v.display_title.replace(suffix, "").strip()
            if not v.mux_asset_id:
                continue
            try:
                update_asset_title(v.mux_asset_id, clean_title)
                v.display_title = clean_title
                updated.append({"vimeo_id": v.vimeo_id, "new_title": clean_title})
            except Exception as e:
                logger.error(f"[Titles] Failed for {v.mux_asset_id}: {e}")
                failed.append({"vimeo_id": v.vimeo_id, "error": str(e)})

        db.commit()

    return {
        "updated": len(updated),
        "failed": len(failed),
        "results": updated,
        "failures": failed,
    }


@router.patch("/rename/{vimeo_id}", summary="Rename a single Mux asset")
def rename_single(vimeo_id: str, new_title: str):
    """
    Updates the Mux meta.title and DB display_title for a single asset.
    """
    with SessionLocal() as db:
        video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
        if not video:
            raise HTTPException(404, f"No record for vimeo_id '{vimeo_id}'")
        if not video.mux_asset_id:
            raise HTTPException(400, "No Mux asset ID on record")
        try:
            update_asset_title(video.mux_asset_id, new_title)
            video.display_title = new_title
            db.commit()
        except Exception as e:
            raise HTTPException(500, f"Mux update failed: {e}")

    return {"vimeo_id": vimeo_id, "new_title": new_title, "mux_asset_id": video.mux_asset_id}
