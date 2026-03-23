from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from datetime import datetime
from app.database.session import Base

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    vimeo_id = Column(String, unique=True, index=True, nullable=False)
    vimeo_title = Column(String, nullable=False)
    vimeo_url = Column(String, nullable=False)
    vimeo_folder_path = Column(String, nullable=True)
    
    mux_asset_id = Column(String, nullable=True)
    mux_playback_id = Column(String, nullable=True)
    mux_stream_url = Column(String, nullable=True)
    
    # --- NEW: Track Verification Fields ---
    captions_count = Column(Integer, default=0)
    captions_languages = Column(String, nullable=True)
    audio_tracks_count = Column(Integer, default=0)
    audio_languages = Column(String, nullable=True)
    # --------------------------------------
    
    status = Column(String, default="pending") # "pending", "ready", "errored"
    created_at = Column(DateTime, default=datetime.utcnow)

class MigrationJob(Base):
    __tablename__ = "migration_jobs"

    id = Column(Integer, primary_key=True, index=True)
    total_videos = Column(Integer, default=0)
    imported_videos = Column(Integer, default=0)
    failed_videos = Column(Integer, default=0)
    status = Column(String, default="running")
    created_at = Column(DateTime, default=datetime.utcnow)

class MigrationError(Base):
    __tablename__ = "migration_errors"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("migration_jobs.id"), nullable=False)
    vimeo_id = Column(String, nullable=False)
    error_message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)