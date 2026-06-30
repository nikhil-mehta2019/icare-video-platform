import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile, Form
from pydantic import BaseModel

from app.services.bulk_attachment_service import attach_tracks_to_asset, find_matching_files, read_excel_rows

router = APIRouter(prefix="/bulk-attachment", tags=["Bulk Attachment"])


class BulkAttachmentRequest(BaseModel):
    excel_path: str
    srt_folder_path: str
    audio_folder_path: str
    asset_id_column: str = "mux_asset_id"
    video_name_column: str = "video_name"
    srt_language: str = "hi"
    audio_language: str = "hi"
    audio_name: str = "Hindi"


@router.post("/attach", summary="Attach SRT and audio files from folders to Mux assets listed in an Excel file")
async def bulk_attach_excel(
    file: UploadFile = File(...),
    srt_folder_path: str = Form(...),
    audio_folder_path: str = Form(...),
    asset_id_column: str = Form("mux_asset_id"),
    video_name_column: str = Form("video_name"),
    srt_language: str = Form("hi"),
    audio_language: str = Form("hi"),
    audio_name: str = Form("Hindi"),
):
    try:
        upload_dir = Path("temp")
        upload_dir.mkdir(exist_ok=True)
        excel_path = upload_dir / file.filename
        with excel_path.open("wb") as out:
            content = await file.read()
            out.write(content)

        rows = read_excel_rows(str(excel_path))
        results = []
        for row in rows:
            asset_id = row.get(asset_id_column)
            video_name = row.get(video_name_column)
            if not asset_id or not video_name:
                continue

            matches = find_matching_files(str(video_name), Path(srt_folder_path), Path(audio_folder_path))
            srt_path = str(matches["srt"]) if matches.get("srt") else None
            audio_path = str(matches["audio"]) if matches.get("audio") else None

            attachment_result = attach_tracks_to_asset(
                str(asset_id),
                srt_path,
                audio_path,
                srt_language=srt_language,
                audio_language=audio_language,
                audio_name=audio_name,
            )
            results.append({
                "asset_id": str(asset_id),
                "video_name": str(video_name),
                "srt_path": srt_path,
                "audio_path": audio_path,
                **attachment_result,
            })

        return {"status": "success", "processed": len(results), "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
