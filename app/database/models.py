from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
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
    
    captions_count = Column(Integer, default=0)
    captions_languages = Column(String, nullable=True)
    audio_tracks_count = Column(Integer, default=0)
    audio_languages = Column(String, nullable=True)
    
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

# --- NEW TABLES FOR ACCESS CONTROL ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Course(Base):
    __tablename__ = "courses"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(String)

class UserCourseAccess(Base):
    __tablename__ = "user_course_access"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))
    access_start = Column(DateTime, default=datetime.utcnow)
    access_end = Column(DateTime)

class VideoProgress(Base):
    __tablename__ = "video_progress"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    vimeo_id = Column(String, ForeignKey("videos.vimeo_id"), nullable=False, index=True)
    
    last_watched_seconds = Column(Integer, default=0)
    is_completed = Column(Boolean, default=False)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    device_type = Column(String, nullable=True)
    session_id = Column(String, nullable=True)

    # Ensure one progress record per user per video
    __table_args__ = (
        UniqueConstraint('user_id', 'vimeo_id', name='uq_user_video_progress'),
    )