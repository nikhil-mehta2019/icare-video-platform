from pydantic import BaseModel
from typing import Optional

class VideoResponse(BaseModel):
    vimeo_id: str
    vimeo_title: str
    mux_asset_id: Optional[str]
    mux_playback_id: Optional[str]
    mux_stream_url: Optional[str]
    
    class Config:
        from_attributes = True  # Changed from orm_mode = True for Pydantic V2

class MigrationResponse(BaseModel):
    status: str
    job_id: int