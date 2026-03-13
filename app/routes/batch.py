from fastapi import APIRouter, UploadFile, File, HTTPException
import shutil
import os

from app.services.batch_service import import_batch

router = APIRouter(prefix="/batch", tags=["Batch"])


@router.post("/activate")
async def activate_batch(file: UploadFile = File(...)):

    try:

        upload_path = f"temp_{file.filename}"

        with open(upload_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = import_batch(upload_path)

        os.remove(upload_path)

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))