from sqlalchemy import Column, Integer, String, DateTime
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
    
    created_at = Column(DateTime, default=datetime.utcnow)

class MigrationJob(Base):
    __tablename__ = "migration_jobs"

    id = Column(Integer, primary_key=True, index=True)
    total_videos = Column(Integer, default=0)
    imported_videos = Column(Integer, default=0)
    failed_videos = Column(Integer, default=0)
    status = Column(String, default="running")
    created_at = Column(DateTime, default=datetime.utcnow)