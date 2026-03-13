from fastapi import APIRouter
from pydantic import BaseModel

from app.services.vimeo_import_service import import_vimeo_video
from app.services.vimeo_service import get_video_download_url

router = APIRouter(prefix="/videos", tags=["Videos"])


class VimeoImportRequest(BaseModel):
    title: str
    vimeo_url: str
    course_id: int
    order: int


@router.post("/import-vimeo")
def import_video(data: VimeoImportRequest):

    # extract vimeo id
    vimeo_id = data.vimeo_url.split("/")[-1]
    
    # get download url from vimeo
    video_url = get_video_download_url(vimeo_id)

    result = import_vimeo_video(
        data.title,
        video_url,
        data.course_id,
        data.order,
        vimeo_id
    )

    return result