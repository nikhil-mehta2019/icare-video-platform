import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database.session import Base, engine
from app.routes import migration, cleanup, titles, manual_upload, webhook, bulk_attachment

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s | %(name)s | %(message)s",
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = os.path.join(BASE_DIR, "temp")
LOGS_DIR = os.path.join(BASE_DIR, "logs")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create DB tables on startup
    Base.metadata.create_all(bind=engine)
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    yield


app = FastAPI(
    title="Mux Migration Tool",
    description=(
        "Lean tool for migrating Vimeo videos to Mux with DRM, "
        "uploading local Hindi videos, cleaning up old assets, "
        "and managing Mux titles."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Serve temp SRT/audio files publicly so Mux can pull them
app.mount("/temp", StaticFiles(directory=TEMP_DIR), name="temp")

# Routes
app.include_router(migration.router)
app.include_router(cleanup.router)
app.include_router(titles.router)
app.include_router(manual_upload.router)
app.include_router(bulk_attachment.router)
app.include_router(webhook.router)


@app.get("/", tags=["Health"])
def root():
    return {
        "service": "mux-migration-tool",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok"}
