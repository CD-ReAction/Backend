from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Project, ProjectMember, Session, SessionCategory, ProjectLike



router = APIRouter(prefix="/projects", tags=["projects"])


# ── 스키마 ──────────────────────────────────────────────

class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = None
    join_code: str = Field(..., min_length=4, max_length=4)  # 사용자가 직접 입력


class ProjectOut(BaseModel):
    project_id: int
    title: str
    description: Optional[str]
    join_code: str
    created_at: str


class JoinRequest(BaseModel):
    join_code: str
    user_id: int  # 추가 (인증 후 토큰에서 자동으로 받을 예정)



class SessionCreate(BaseModel):
    title: Optional[str] = None
    s_category: SessionCategory 


class SessionOut(BaseModel):
    session_id: int
    project_id: int
    title: Optional[str]
    s_category: SessionCategory
    in_progress: bool  # True = 진행중, False = 진행 완료 (영상 업로드 완료 시 자동 False)
    created_at: str


# ── 헬퍼 ────────────────────────────────────────────────

def _project_to_out(p: Project) -> ProjectOut:
    return ProjectOut(
        project_id=p.project_id,
        title=p.title,
        description=p.description,
        join_code=p.join_code,
        created_at=p.created_at.isoformat(),
    )


# ── 프로젝트 엔드포인트 ──────────────────────────────────

@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    user_id: int = 1,  # ← 추가
    db: AsyncSession = Depends(get_db),
):
    """프로젝트 생성 (사용자가 직접 join_code 지정)"""

    code = body.join_code.strip().upper()

    # 코드 중복 검사
    result = await db.execute(select(Project).where(Project.join_code == code))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 사용 중인 코드예요. 다른 코드를 입력해주세요")

    project = Project(
        title=body.title,
        description=body.description,
        join_code=code,
    )
    db.add(project)
    await db.flush()  # project_id 확보

    # 생성자를 멤버로 자동 등록
    db.add(ProjectMember(
        project_id=project.project_id,
        user_id=user_id,
    ))
    await db.flush()

    return _project_to_out(project)


@router.get("", response_model=List[ProjectOut])
async def get_my_projects(
    user_id: int = 1,  # ← 추가
    db: AsyncSession = Depends(get_db)
):

    result = await db.execute(
        select(Project)
        .join(ProjectMember, ProjectMember.project_id == Project.project_id)
        .where(ProjectMember.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    return [_project_to_out(p) for p in projects]


@router.post("/join", response_model=ProjectOut)
async def join_project(
    body: JoinRequest,
    db: AsyncSession = Depends(get_db),
):
    user_id = body.user_id 

    code = body.join_code.strip().upper()
    if len(code) != 4:
        raise HTTPException(status_code=400, detail="코드는 4자리여야 해요")

    # 1. 코드로 프로젝트 찾기
    result = await db.execute(select(Project).where(Project.join_code == code))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="유효하지 않은 코드예요")

    # 2. 이미 참여 중인지 확인
    result = await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == project.project_id,
            ProjectMember.user_id == user_id,
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="이미 참여 중인 프로젝트예요")

    # 3. 멤버로 등록
    db.add(ProjectMember(
        project_id=project.project_id,
        user_id=user_id,
    ))
    await db.flush()

    return _project_to_out(project)


# ── 세션 엔드포인트 ──────────────────────────────────────

@router.post("/{project_id}/sessions", response_model=SessionOut, status_code=201)
async def create_session(
    project_id: int,
    body: SessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """세션 생성"""
    session = Session(
        project_id=project_id,
        title=body.title,
        s_category=body.s_category,
        in_progress=True,
    )
    db.add(session)
    await db.flush()

    return SessionOut(
        session_id=session.session_id,
        s_category=session.s_category,
        project_id=session.project_id,
        title=session.title,
        in_progress=session.in_progress,
        created_at=session.created_at.isoformat(),
    )

'''
@router.get("/{project_id}/sessions", response_model=List[SessionOut])
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
            in_progress=s.in_progress,
            created_at=s.created_at.isoformat(),
        )
        for s in sessions
    ]
'''


@router.get("/{project_id}/sessions", response_model=List[SessionOut])
async def list_sessions(
    project_id: int,
    category: Optional[SessionCategory] = Query(
        None,
        description="카테고리로 필터링 (선택). 미지정 시 전체 조회.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """프로젝트의 세션 목록 조회 (카테고리 필터 가능)"""
    stmt = (
        select(Session)
        .where(Session.project_id == project_id)
        .order_by(Session.created_at.desc())
    )
    
    if category is not None:
        stmt = stmt.where(Session.s_category == category)
    
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    
    return [
        SessionOut(
            session_id=s.session_id,
            s_category=s.s_category,
            project_id=s.project_id,
            title=s.title,
            in_progress=s.in_progress,
            created_at=s.created_at.isoformat(),
        )
        for s in sessions
    ]


@router.post("/{session_id}/rehearsal/start")
async def start_rehearsal(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Session).where(Session.session_id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    if not session.rehearsal_started:
        session.rehearsal_started = True
        session.rehearsal_started_at = datetime.utcnow()
        await db.commit()

    return {
        "db_session_id": session_id,
        "started": session.rehearsal_started,
        "started_at": session.rehearsal_started_at.isoformat() if session.rehearsal_started_at else None,
    }


@router.get("/{session_id}/rehearsal/status")
async def get_rehearsal_status(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Session).where(Session.session_id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    return {
        "db_session_id": session_id,
        "started": session.rehearsal_started or False,
        "started_at": session.rehearsal_started_at.isoformat() if session.rehearsal_started_at else None,
    }


# 좋아요 토글
@router.post("/{project_id}/like")
async def toggle_like(
    project_id: int,
    user_id: int = 1,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProjectLike).where(
            ProjectLike.project_id == project_id,
            ProjectLike.user_id == user_id,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        await db.delete(existing)
        await db.commit()
        return {"liked": False}
    else:
        db.add(ProjectLike(project_id=project_id, user_id=user_id))
        await db.commit()
        return {"liked": True}


# 좋아요한 프로젝트 목록
@router.get("/liked", response_model=List[ProjectOut])
async def get_liked_projects(
    user_id: int = 1,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Project)
        .join(ProjectLike, ProjectLike.project_id == Project.project_id)
        .where(ProjectLike.user_id == user_id)
        .order_by(ProjectLike.created_at.desc())
    )
    projects = result.scalars().all()
    return [_project_to_out(p) for p in projects]