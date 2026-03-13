from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    accesses = relationship("UserCourseAccess", back_populates="user")


class Course(Base):
    __tablename__ = "courses"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(String)

    videos = relationship("Video", back_populates="course")


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)

    course_id = Column(Integer, ForeignKey("courses.id"))

    mux_asset_id = Column(String)
    playback_id = Column(String)

    vimeo_id = Column(String, unique=True, nullable=True)
    vimeo_url = Column(String, nullable=True)

    order = Column(Integer)

    course = relationship("Course", back_populates="videos")


class UserCourseAccess(Base):
    __tablename__ = "user_course_access"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"))
    course_id = Column(Integer, ForeignKey("courses.id"))

    access_start = Column(DateTime)
    access_end = Column(DateTime)

    user = relationship("User", back_populates="accesses")


class MigrationJob(Base):
    __tablename__ = "migration_jobs"

    id = Column(Integer, primary_key=True, index=True)

    course_id = Column(Integer)

    total_videos = Column(Integer, default=0)
    imported_videos = Column(Integer, default=0)
    failed_videos = Column(Integer, default=0)

    status = Column(String, default="running")

    created_at = Column(DateTime, default=datetime.utcnow)