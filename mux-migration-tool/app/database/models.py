from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from datetime import datetime
from app.database.session import Base


class Video(Base):
    """One record per migrated (or manually uploaded) video."""
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)

    # Source identifiers
    # For Vimeo migrations  : vimeo_id  = "<vimeo_id>" (no suffix in DB key — title carries suffix if any)
    # For manual uploads    : vimeo_id  = "manual_<slug>"  (no Vimeo equivalent)
    vimeo_id = Column(String(100), unique=True, index=True, nullable=False)
    vimeo_title = Column(String(512), nullable=False)       # clean title shown to students
    display_title = Column(String(512), nullable=True)      # title with temp suffix if set (internal only)
    vimeo_url = Column(String(2000), nullable=False, default="")
    vimeo_folder_path = Column(String(500), nullable=True)
    source = Column(String(20), default="vimeo")            # "vimeo" | "manual"

    # Mux identifiers
    mux_asset_id = Column(String(100), nullable=True)
    mux_playback_id = Column(String(100), nullable=True)         # public — for Mux dashboard preview
    mux_signed_playback_id = Column(String(100), nullable=True)  # signed — JWT-protected, used by app player
    mux_drm_playback_id = Column(String(100), nullable=True)     # DRM-protected playback
    mux_stream_url = Column(String(2000), nullable=True)

    # Track metadata
    captions_count = Column(Integer, default=0)
    captions_languages = Column(String(500), nullable=True)
    audio_tracks_count = Column(Integer, default=0)
    audio_languages = Column(String(500), nullable=True)

    # State
    status = Column(String(50), default="pending")  # pending | processing | ready | errored
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MigrationJob(Base):
    """Tracks the progress of a folder-migration run."""
    __tablename__ = "migration_jobs"

    id = Column(Integer, primary_key=True, index=True)
    folder_url = Column(String(2000), nullable=True)
    title_suffix = Column(String(50), nullable=True)
    total_videos = Column(Integer, default=0)
    imported_videos = Column(Integer, default=0)
    failed_videos = Column(Integer, default=0)
    status = Column(String(50), default="running")  # running | completed | failed | cancelled
    created_at = Column(DateTime, default=datetime.utcnow)


class MigrationError(Base):
    """Records individual video failures within a migration job."""
    __tablename__ = "migration_errors"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("migration_jobs.id"), nullable=False)
    vimeo_id = Column(String(100), nullable=False)
    error_message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
