from datetime import datetime
import enum # 세션 카테고리를 Enum으로 정의하기 위해 enum 모듈을 import


from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, Float,
    UniqueConstraint, Enum
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    memberships = relationship("ProjectMember", back_populates="user")


class Project(Base):
    __tablename__ = "projects"

    project_id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    join_code = Column(String(4), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    members = relationship("ProjectMember", back_populates="project", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="project")
    actors = relationship("Actor", back_populates="project")


class ProjectMember(Base):
    """User ↔ Project 다:다 중간 테이블"""
    __tablename__ = "project_members"

    member_id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    joined_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="members")
    user = relationship("User", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_user"),
    )


class Actor(Base):
    __tablename__ = "actors"

    actor_id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    # INSERT 시점엔 null → 직후 "배우 {actor_id}"로 UPDATE (사용자 수정 가능)
    name = Column(String, nullable=True)
    face_embedding = Column(ARRAY(Float), nullable=True)  # Postgres FLOAT8[]
    thumbnail_s3_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="actors")
    video_links = relationship("VideoActor", back_populates="actor", cascade="all, delete-orphan")

class SessionCategory(str, enum.Enum):
    CATEGORY_A = "장면별 연습"
    CATEGORY_B = "워크쓰루"
    CATEGORY_C = "런쓰루"
    CATEGORY_D = "텐투텐"

class VideoActor(Base):
    """어느 영상에 어느 배우가 나왔는지 + 그 영상에서 처음 등장한 배우인지"""
    __tablename__ = "video_actors"

    video_actor_id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.video_id", ondelete="CASCADE"), nullable=False)
    actor_id = Column(Integer, ForeignKey("actors.actor_id", ondelete="CASCADE"), nullable=False)
    is_new_in_video = Column(Boolean, default=False, nullable=False)

    video = relationship("Video", back_populates="actor_links")
    actor = relationship("Actor", back_populates="video_links")

    __table_args__ = (
        UniqueConstraint("video_id", "actor_id", name="uq_video_actor"),
    )

class SessionCategory(str, enum.Enum):
    CATEGORY_A = "장면별 연습"
    CATEGORY_B = "워크쓰루"
    CATEGORY_C = "런쓰루"
    CATEGORY_D = "텐투텐"

class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.project_id"), nullable=False)
    title = Column(String, nullable=True)
    s_category = Column(Enum(SessionCategory), nullable=False)
    in_progress = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="sessions")
    video = relationship("Video", back_populates="session", uselist=False)
    feedbacks = relationship("Feedback", back_populates="session")


class Video(Base):
    __tablename__ = "videos"

    video_id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    s3_key = Column(String, nullable=True)
    s3_url = Column(String, nullable=True)
    analysis_status = Column(String, default="pending")
    analysis_result = Column(Text, nullable=True)
    record_started_at = Column(DateTime, nullable=True)
    record_ended_at = Column(DateTime, nullable=True)

    session = relationship("Session", back_populates="video")
    actor_links = relationship("VideoActor", back_populates="video", cascade="all, delete-orphan")


class Feedback(Base):
    __tablename__ = "feedbacks"

    feedback_id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    content = Column(Text, nullable=False)
    video_offset_seconds = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("Session", back_populates="feedbacks")

class CameraSession(Base): #camera-connection
    __tablename__ = "camera_sessions"

    id = Column(String, primary_key=True)
    db_session_id = Column(Integer, nullable=False)
    code = Column(String, nullable=False)
    camera_url = Column(String, nullable=False)
    status = Column(String, default="yet")
    expires_at = Column(DateTime, nullable=False)
    connected_at = Column(DateTime, nullable=True)
    video_url = Column(String, nullable=True)