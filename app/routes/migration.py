from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app.services.vimeo_account_migration import migrate_vimeo_account

router = APIRouter(prefix="/migration", tags=["Migration"])


class MigrationRequest(BaseModel):
    course_id: int


@router.post("/vimeo-account")
def migrate(data: MigrationRequest, background_tasks: BackgroundTasks):

    background_tasks.add_task(
        migrate_vimeo_account,
        data.course_id
    )

    return {
        "status": "migration started"
    }