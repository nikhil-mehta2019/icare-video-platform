import os
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.services.attachment_service import (
    _find_match,
    _normalize,
    attach_tracks_to_asset,
    read_excel_rows,
    TEMP_AUDIO_DIR,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attachment", tags=["Bulk Attachment"])


@router.post("/attach", summary="Bulk-attach audio and/or SRT files to existing Mux assets via Excel mapping")
async def bulk_attach(
    excel_file: UploadFile = File(..., description="Excel (.xlsx) with asset_id and video_name columns"),
    audio_files: Optional[List[UploadFile]] = File(None, description="Audio files (.mp3 / .m4a / .aac)"),
    srt_files: Optional[List[UploadFile]] = File(None, description="SRT subtitle files"),
    asset_id_column: str = Form("mux_asset_id", description="Column name for Mux asset ID"),
    video_name_column: str = Form("video_name", description="Column name for video title used for filename matching"),
    audio_language: str = Form("hi", description="Language code for audio tracks (e.g. hi, es, sw)"),
    audio_name: str = Form("Hindi", description="Display name for the audio track in Mux"),
    srt_language: str = Form("hi", description="Language code for subtitle tracks"),
):
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    saved_paths: list[str] = []

    try:
        # Save Excel
        excel_path = os.path.join(TEMP_AUDIO_DIR, excel_file.filename)
        content = await excel_file.read()
        with open(excel_path, "wb") as f:
            f.write(content)
        saved_paths.append(excel_path)

        # Save audio files and build normalized→path map
        audio_map: dict[str, str] = {}
        for uf in (audio_files or []):
            dest = os.path.join(TEMP_AUDIO_DIR, uf.filename)
            data = await uf.read()
            with open(dest, "wb") as f:
                f.write(data)
            saved_paths.append(dest)
            audio_map[_normalize(uf.filename)] = dest

        # Save SRT files and build normalized→path map
        srt_map: dict[str, str] = {}
        for uf in (srt_files or []):
            dest = os.path.join(TEMP_AUDIO_DIR, uf.filename)
            data = await uf.read()
            with open(dest, "wb") as f:
                f.write(data)
            saved_paths.append(dest)
            srt_map[_normalize(uf.filename)] = dest

        # Read Excel rows
        try:
            rows = await run_in_threadpool(read_excel_rows, excel_path, asset_id_column, video_name_column)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to read Excel: {e}")

        if not rows:
            raise HTTPException(status_code=400, detail="No valid rows found in Excel (check column names).")

        results = []
        for row in rows:
            asset_id   = row["asset_id"]
            video_name = row["video_name"]

            matched_audio = _find_match(video_name, audio_map) if audio_map else None
            matched_srt   = _find_match(video_name, srt_map)   if srt_map   else None

            if not matched_audio and not matched_srt:
                results.append({
                    "asset_id": asset_id,
                    "video_name": video_name,
                    "status": "skipped",
                    "reason": "no matching audio or SRT file found",
                })
                continue

            outcome = await run_in_threadpool(
                attach_tracks_to_asset,
                asset_id,
                matched_audio,
                matched_srt,
                audio_language,
                audio_name,
                srt_language,
            )

            status = "error" if outcome["errors"] and not outcome["audio"] and not outcome["srt"] else "success"
            results.append({
                "asset_id": asset_id,
                "video_name": video_name,
                "matched_audio": os.path.basename(matched_audio) if matched_audio else None,
                "matched_srt": os.path.basename(matched_srt) if matched_srt else None,
                "audio": outcome["audio"],
                "srt": outcome["srt"],
                "errors": outcome["errors"],
                "status": status,
            })

        success  = sum(1 for r in results if r["status"] == "success")
        skipped  = sum(1 for r in results if r["status"] == "skipped")
        errors   = sum(1 for r in results if r["status"] == "error")

        return {
            "processed": len(rows),
            "success": success,
            "skipped": skipped,
            "errors": errors,
            "results": results,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Attachment] Unexpected error during bulk attach")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up Excel and any unmatched uploaded files that weren't consumed by attach_tracks_to_asset
        for path in saved_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
