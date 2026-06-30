import os
import logging

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

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".opus", ".webm", ".ogg"}
SRT_EXTENSIONS   = {".srt", ".vtt"}


def _build_file_map(folder: str, extensions: set[str]) -> dict[str, str]:
    file_map = {}
    if not os.path.isdir(folder):
        raise ValueError(f"Folder not found on server: {folder}")
    for fname in os.listdir(folder):
        if os.path.splitext(fname)[1].lower() in extensions:
            file_map[_normalize(fname)] = os.path.join(folder, fname)
    return file_map


@router.post("/attach", summary="Bulk-attach audio and/or SRT files to existing Mux assets via Excel mapping")
async def bulk_attach(
    excel_file: UploadFile = File(..., description="Excel (.xlsx) with asset_id and video_name columns"),
    audio_folder: str = Form(None, description="Absolute path to folder containing audio files on the server"),
    srt_folder: str = Form(None, description="Absolute path to folder containing SRT files on the server"),
    asset_id_column: str = Form("mux_asset_id", description="Column name for Mux asset ID"),
    video_name_column: str = Form("video_name", description="Column name for video title used for filename matching"),
    audio_language: str = Form("hi", description="Language code for audio tracks (e.g. hi, es, sw)"),
    audio_name: str = Form("Hindi", description="Display name for the audio track in Mux"),
    srt_language: str = Form("hi", description="Language code for subtitle tracks"),
):
    if not audio_folder and not srt_folder:
        raise HTTPException(status_code=400, detail="Provide at least one of audio_folder or srt_folder.")

    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    excel_path = os.path.join(TEMP_AUDIO_DIR, excel_file.filename)

    try:
        # Save uploaded Excel to temp dir
        content = await excel_file.read()
        with open(excel_path, "wb") as f:
            f.write(content)

        # Build file maps from server folders
        try:
            audio_map = _build_file_map(audio_folder, AUDIO_EXTENSIONS) if audio_folder else {}
            srt_map   = _build_file_map(srt_folder,   SRT_EXTENSIONS)   if srt_folder   else {}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if audio_folder and not audio_map:
            raise HTTPException(status_code=400, detail=f"No audio files found in: {audio_folder}")
        if srt_folder and not srt_map:
            raise HTTPException(status_code=400, detail=f"No SRT files found in: {srt_folder}")

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

        success = sum(1 for r in results if r["status"] == "success")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        errors  = sum(1 for r in results if r["status"] == "error")

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
        try:
            if os.path.exists(excel_path):
                os.remove(excel_path)
        except Exception:
            pass
