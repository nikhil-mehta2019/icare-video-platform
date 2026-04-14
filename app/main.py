import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import playback, videos, migration, webhook, batch
from app.database.session import Base, engine

TEMP_AUDIO_DIR = "/tmp/mux_audio"
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

app = FastAPI(title="iCare Video Migration Platform")

# Serve temp audio files publicly so Mux can fetch them for track attachment
app.mount("/temp-audio", StaticFiles(directory=TEMP_AUDIO_DIR), name="temp-audio")

# Create tables based on the updated models
Base.metadata.create_all(bind=engine)

app.include_router(playback.router)
app.include_router(videos.router)
app.include_router(migration.router)
app.include_router(webhook.router)
app.include_router(batch.router)

@app.get("/")
def home():
    return {"message": "iCare Video Migration Infrastructure Running"}