"""
feedback.py (v2)
────────────────
대본(PDF) 플로우용 피드백 API — 대본 앵커(script_page / script_x / script_y) 포함.

- 대본 없는 프로젝트는 기존 v1 사용 (UI 분기). 삭제는 v1 그대로 사용.
- 앵커 좌표는 페이지 내 정규화(0~1) — 화면 크기와 무관하게 같은 위치 렌더링.
- WS 이벤트는 v1과 동일한 feedback.created / feedback.updated 재사용.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.realtime import manager
from app.models.models import Actor, Feedback, FeedbackActor, Session
from app.api.v1.endpoints.feedback import (
    _actor_names,
    _feedback_event_payload,
    _load_actor_ids,
    _project_id_of_session,
)

router = APIRouter(prefix="/sessions/{session_id}/feedbacks", tags=["feedback-v2"])
project_router = APIRouter(prefix="/projects", tags=["feedback-v2"])


# ── 스키마 ──────────────────────────────────────────────

class FeedbackCreateV2(BaseModel):
    content: str
    video_offset_seconds: Optional[float] = None
    actor_ids: List[int] = []
    # 대본 앵커 (클릭 위치). 페이지는 1-base, 좌표는 페이지 내 0~1 정규화
    script_page: Optional[int] = Field(default=None, ge=1)
    script_x: Optional[float] = Field(default=None, ge=0, le=1)
    script_y: Optional[float] = Field(default=None, ge=0, le=1)


class FeedbackUpdateV2(BaseModel):
    content: Optional[str] = None
    video_offset_seconds: Optional[float] = None
    actor_ids: Optional[List[int]] = None
    script_page: Optional[int] = Field(default=None, ge=1)
    script_x: Optional[float] = Field(default=None, ge=0, le=1)
    script_y: Optional[float] = Field(default=None, ge=0, le=1)


class FeedbackOutV2(BaseModel):
    feedback_id: int
    session_id: int
    created_by_user_id: int
    content: str
    video_offset_seconds: Optional[float]
    actor_ids: List[int]
    script_page: Optional[int]
    script_x: Optional[float]
    script_y: Optional[float]
    created_at: str


def _to_out(feedback: Feedback, actor_ids: List[int]) -> FeedbackOutV2:
    return FeedbackOutV2(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        created_by_user_id=feedback.created_by_user_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        actor_ids=actor_ids,
        script_page=feedback.script_page,
        script_x=feedback.script_x,
        script_y=feedback.script_y,
        created_at=feedback.created_at.isoformat(),
    )


async def _validate_actor_ids(db: AsyncSession, actor_ids: List[int]) -> None:
    if not actor_ids:
        return
    res = await db.execute(
        select(Actor.actor_id).where(Actor.actor_id.in_(actor_ids))
    )
    existing = {r[0] for r in res.all()}
    missing = [aid for aid in actor_ids if aid not in existing]
    if missing:
        raise HTTPException(status_code=400, detail=f"존재하지 않는 배우: {missing}")


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("", response_model=FeedbackOutV2, status_code=201)
async def create_feedback_v2(
    session_id: int,
    body: FeedbackCreateV2,
    user_id: int = Query(..., description="작성자 user_id"),
    db: AsyncSession = Depends(get_db),
):
    """피드백 작성 (대본 클릭 앵커 포함).

    대본 클릭 한 번 = (영상 시점, 대본 위치) 매핑:
    프론트가 클릭 순간의 녹화 경과 시간을 video_offset_seconds로,
    클릭한 페이지/좌표를 script_page/x/y로 채워서 보냄.
    """
    feedback = Feedback(
        session_id=session_id,
        created_by_user_id=user_id,
        content=body.content,
        video_offset_seconds=body.video_offset_seconds,
        script_page=body.script_page,
        script_x=body.script_x,
        script_y=body.script_y,
    )
    db.add(feedback)
    await db.flush()

    actor_ids = list(dict.fromkeys(body.actor_ids))
    await _validate_actor_ids(db, actor_ids)
    for aid in actor_ids:
        db.add(FeedbackActor(feedback_id=feedback.feedback_id, actor_id=aid))
    await db.flush()

    # commit 후 broadcast (payload는 commit된 DB 상태)
    project_id = await _project_id_of_session(db, session_id)
    actor_names = await _actor_names(db, actor_ids)
    await db.commit()
    await manager.broadcast(
        "feedback.created",
        project_id,
        session_id,
        _feedback_event_payload(feedback, actor_ids, actor_names),
    )

    return _to_out(feedback, actor_ids)


@router.get("", response_model=List[FeedbackOutV2])
async def get_feedbacks_v2(
    session_id: int,
    actor_ids: List[int] = Query(default=[]),
    user_id: Optional[int] = Query(default=None, description="지정 시 해당 사용자가 작성한 피드백만"),
    db: AsyncSession = Depends(get_db),
):
    """세션 피드백 목록 (대본 앵커 포함). 필터 동작은 v1과 동일."""
    query = select(Feedback).where(Feedback.session_id == session_id)
    if actor_ids:
        sub = select(FeedbackActor.feedback_id).where(
            FeedbackActor.actor_id.in_(actor_ids)
        )
        query = query.where(Feedback.feedback_id.in_(sub))
    if user_id is not None:
        query = query.where(Feedback.created_by_user_id == user_id)
    query = query.order_by(Feedback.created_at)

    result = await db.execute(query)
    feedbacks = result.scalars().all()
    if not feedbacks:
        return []

    actors_by_feedback = await _load_actor_ids(db, [f.feedback_id for f in feedbacks])
    return [
        _to_out(f, actors_by_feedback.get(f.feedback_id, []))
        for f in feedbacks
    ]


@router.patch("/{feedback_id}", response_model=FeedbackOutV2)
async def update_feedback_v2(
    session_id: int,
    feedback_id: int,
    body: FeedbackUpdateV2,
    user_id: int = Query(..., description="요청자 user_id (작성자 본인만 수정 가능)"),
    db: AsyncSession = Depends(get_db),
):
    """피드백 부분 수정 (앵커 포함). 작성자 본인이 아니면 403."""
    result = await db.execute(
        select(Feedback).where(
            Feedback.feedback_id == feedback_id,
            Feedback.session_id == session_id,
        )
    )
    feedback = result.scalar_one_or_none()
    if not feedback:
        raise HTTPException(status_code=404, detail="피드백을 찾을 수 없어요")
    if feedback.created_by_user_id != user_id:
        raise HTTPException(status_code=403, detail="작성자만 수정할 수 있어요")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="수정할 내용이 없어요")

    new_actor_ids = update_data.pop("actor_ids", None)
    for key, value in update_data.items():
        setattr(feedback, key, value)

    if new_actor_ids is not None:
        new_actor_ids = list(dict.fromkeys(new_actor_ids))
        await _validate_actor_ids(db, new_actor_ids)
        cur = await db.execute(
            select(FeedbackActor).where(FeedbackActor.feedback_id == feedback_id)
        )
        for link in cur.scalars().all():
            await db.delete(link)
        await db.flush()
        for aid in new_actor_ids:
            db.add(FeedbackActor(feedback_id=feedback_id, actor_id=aid))

    await db.flush()
    await db.refresh(feedback)

    actors_by_feedback = await _load_actor_ids(db, [feedback_id])
    final_actor_ids = actors_by_feedback.get(feedback_id, [])

    project_id = await _project_id_of_session(db, session_id)
    actor_names = await _actor_names(db, final_actor_ids)
    await db.commit()
    await manager.broadcast(
        "feedback.updated",
        project_id,
        session_id,
        _feedback_event_payload(feedback, final_actor_ids, actor_names),
    )

    return _to_out(feedback, final_actor_ids)


# ── 프로젝트 전체 앵커 피드백 (다시보기 누적 모드) ────────

@project_router.get(
    "/{project_id}/script/feedbacks",
    response_model=List[FeedbackOutV2],
)
async def get_project_script_feedbacks(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """프로젝트 전체 세션의 대본 앵커 피드백 조회.

    다시보기 누적 토글(ON)용:
    - 각 항목의 session_id로 프론트가 세션별 색 배정
    - OFF(이번 세션만)는 프론트에서 현재 session_id로 필터
    - 앵커 없는 피드백(script_page=null)은 제외
    """
    result = await db.execute(
        select(Feedback)
        .join(Session, Session.session_id == Feedback.session_id)
        .where(
            Session.project_id == project_id,
            Feedback.script_page.isnot(None),
        )
        .order_by(Feedback.created_at)
    )
    feedbacks = result.scalars().all()
    if not feedbacks:
        return []

    actors_by_feedback = await _load_actor_ids(db, [f.feedback_id for f in feedbacks])
    return [
        _to_out(f, actors_by_feedback.get(f.feedback_id, []))
        for f in feedbacks
    ]
