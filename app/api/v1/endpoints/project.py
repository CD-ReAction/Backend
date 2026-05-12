"""
project.py
──────────
프로젝트 & 세션 생성/조회
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Project, Session

router = APIRouter(prefix="/projects", tags=["projects"])



# ── 스키마 ──────────────────────────────────────────────

class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = None


class ProjectOut(BaseModel):
    project_id: int
    title: str
    description: Optional[str]
    created_at: str


class SessionCreate(BaseModel):
    title: Optional[str] = None


class SessionOut(BaseModel):
    session_id: int
    project_id: int
    title: Optional[str]
    created_at: str


# ── 프로젝트 엔드포인트 ──────────────────────────────────

@router.post("", ...)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
):
    """프로젝트 생성"""
    project = Project(
        user_id=1,  # TODO: 인증 후 실제 user_id 사용
        title=body.title,
        description=body.description,
    )
    db.add(project)
    await db.flush()

    return ProjectOut(
        project_id=project.project_id,
        title=project.title,
        description=project.description,
        created_at=project.created_at.isoformat(),
    )


@router.get("/api/v1/projects", response_model=List[ProjectOut])
async def get_projects(db: AsyncSession = Depends(get_db)):
    """프로젝트 목록 조회"""
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    projects = result.scalars().all()

    return [
        ProjectOut(
            project_id=p.project_id,
            title=p.title,
            description=p.description,
            created_at=p.created_at.isoformat(),
        )
        for p in projects
    ]


# ── 세션 엔드포인트 ──────────────────────────────────────

@router.post("/api/v1/projects/{project_id}/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    project_id: int,
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """세션 생성"""
    session = Session(
        project_id=project_id,
        title=body.title,
        in_progress=True,
    )
    db.add(session)
    await db.flush()

    return SessionOut(
        session_id=session.session_id,
        project_id=session.project_id,
        title=session.title,
        created_at=session.created_at.isoformat(),
    )


@router.get("/api/v1/projects/{project_id}/sessions", response_model=List[SessionOut])
async def get_sessions(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """세션 목록 조회"""
    result = await db.execute(
        select(Session)
        .where(Session.project_id == project_id)
        .order_by(Session.created_at.desc())
    )
    sessions = result.scalars().all()

    return [
        SessionOut(
            session_id=s.session_id,
            project_id=s.project_id,
            title=s.title,
            created_at=s.created_at.isoformat(),
        )
        for s in sessions
    ]