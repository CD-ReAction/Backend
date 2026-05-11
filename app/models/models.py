from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, Float
from sqlalchemy.orm import relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    projects = relationship("Project", back_populates="owner")


class Project(Base):
    __tablename__ = "projects"

    project_id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    owner = relationship("User", back_populates="projects")
    sessions = relationship("Session", back_populates="project")
    actors = relationship("Actor", back_populates="project")


class Actor(Base):
    __tablename__ = "actors"

    actor_id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    name = Column(String, nullable=False)
    face_embedding = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="actors")


class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    title = Column(String, nullable=True)
    in_progress = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="sessions")
    video = relationship("Video", back_populates="session", uselist=False)
    feedbacks = relationship("Feedback", back_populates="session")


class Video(Base):
    __tablename__ = "videos"

    video_id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    s3_key = Column(String, nullable=True)       # S3 저장 경로
    s3_url = Column(String, nullable=True)        # S3 URL
    analysis_status = Column(String, default="pending")  # pending/processing/done/failed
    analysis_result = Column(Text, nullable=True) # JSON string (face-analysis 결과)
    record_started_at = Column(DateTime, nullable=True)
    record_ended_at = Column(DateTime, nullable=True)

    session = relationship("Session", back_populates="video")


class Feedback(Base):
    __tablename__ = "feedbacks"

    feedback_id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    content = Column(Text, nullable=False)
    video_offset_seconds = Column(Float, nullable=True)  # 영상 타임스탬프
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("Session", back_populates="feedbacks")