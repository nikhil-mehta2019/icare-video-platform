from pydantic import BaseModel

class VimeoImportRequest(BaseModel):
    title: str
    vimeo_url: str