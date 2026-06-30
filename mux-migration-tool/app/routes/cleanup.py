import logging
from fastapi import APIRouter, HTTPException
from app.database.session import SessionLocal
from app.database.models import Video
from app.services.mux_service import delete_asset, list_all_assets, update_asset_title

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cleanup", tags=["Cleanup"])


@router.delete("/old-assets", summary="Delete old Mux assets that don't match the new suffix")
def delete_old_assets(new_suffix: str, dry_run: bool = False):
    """
    Deletes all Mux assets (and their DB records) whose `display_title` does NOT
    end with `new_suffix`.

    Use this after confirming a new batch of migrated assets is `ready`.

    - new_suffix : e.g. " (New)" — assets whose titles contain this are kept.
    - dry_run    : if true, returns what would be deleted without actually deleting.

    Example: DELETE /cleanup/old-assets?new_suffix=%20(New)
    """
    with SessionLocal() as db:
        to_delete = db.query(Video).filter(
            Video.source == "vimeo",
            ~Video.display_title.contains(new_suffix)
        ).all()

        if dry_run:
            return {
                "dry_run": True,
                "would_delete": len(to_delete),
                "assets": [{"vimeo_id": v.vimeo_id, "title": v.display_title,
                             "mux_asset_id": v.mux_asset_id} for v in to_delete],
            }

        deleted, failed = [], []
        for v in to_delete:
            if v.mux_asset_id:
                try:
                    delete_asset(v.mux_asset_id)
                    deleted.append(v.vimeo_id)
                except Exception as e:
                    logger.error(f"[Cleanup] Failed to delete Mux asset {v.mux_asset_id}: {e}")
                    failed.append({"vimeo_id": v.vimeo_id, "error": str(e)})
                    continue
            db.delete(v)

        db.commit()

    return {
        "deleted": len(deleted),
        "failed": len(failed),
        "deleted_ids": deleted,
        "failures": failed,
    }


@router.delete("/single/{vimeo_id}", summary="Delete one Mux asset + DB record by vimeo_id")
def delete_single(vimeo_id: str):
    """
    Deletes the Mux asset and DB record for a specific vimeo_id.
    Useful for removing a single errored or outdated video.
    """
    with SessionLocal() as db:
        video = db.query(Video).filter(Video.vimeo_id == vimeo_id).first()
        if not video:
            raise HTTPException(404, f"No record found for vimeo_id '{vimeo_id}'")

        if video.mux_asset_id:
            try:
                delete_asset(video.mux_asset_id)
            except Exception as e:
                raise HTTPException(500, f"Mux delete failed: {e}")

        db.delete(video)
        db.commit()

    return {"status": "deleted", "vimeo_id": vimeo_id}
