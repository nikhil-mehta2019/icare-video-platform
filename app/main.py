import os
import logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import playback, videos, migration, webhook, batch, auth
from app.database.session import Base, engine

# ── Directories ──────────────────────────────────────────────────────────────
BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR        = os.path.join(BASE_DIR, "logs")
TEMP_AUDIO_DIR  = os.path.join(BASE_DIR, "temp_audio")

os.makedirs(LOGS_DIR,       exist_ok=True)
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_FILE = os.path.join(LOGS_DIR, "app.log")

formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Rotating file handler — max 10 MB per file, keep 5 backups
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.INFO)

# Apply to root logger so ALL modules inherit it
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.info(f"Logging initialised — writing to {LOG_FILE}")
logger.info(f"Temp audio directory: {TEMP_AUDIO_DIR}")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="iCare Video Migration Platform")

# Serve temp audio files publicly so Mux can fetch them for track attachment
app.mount("/temp-audio", StaticFiles(directory=TEMP_AUDIO_DIR), name="temp-audio")

# Create DB tables
Base.metadata.create_all(bind=engine)

app.include_router(auth.router)
app.include_router(playback.router)
app.include_router(videos.router)
app.include_router(migration.router)
app.include_router(webhook.router)
app.include_router(batch.router)

@app.get("/")
def home():
    return {"message": "iCare Video Migration Infrastructure Running"}
