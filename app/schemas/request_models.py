from pydantic import BaseModel
from typing import Optional

class VimeoImportRequest(BaseModel):
    title: str
    vimeo_url: str

class BulkMigrationRequest(BaseModel):
    limit: Optional[int] = None