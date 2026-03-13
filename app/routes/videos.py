from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.schemas.request_models import VimeoImportRequest
from app.services.migration_service import process_single_video
from app.database.session import SessionLocal

router = APIRouter(prefix="/videos", tags=["Videos"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest, db: Session = Depends(get_db)):
    try:
        # Automatically extract Vimeo ID, stripping trailing slashes and query parameters
        vimeo_id = data.vimeo_url.rstrip("/").split("/")[-1].split("?")[0]
        
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