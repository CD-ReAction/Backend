"""
feedback.py
───────────
피드백 작성, 조회, 수정, 삭제 (배우 mention 다대다)
"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Actor, Feedback, FeedbackActorMention, Session

router = APIRouter(prefix="/sessions/{session_id}/feedbacks", tags=["feedback"])


# ── 스키마 ──────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    content: str
    video_offset_seconds: Optional[float] = None
    actor_ids: List[int] = Field(..., min_length=1)


class FeedbackUpdate(BaseModel):
    content: Optional[str] = None
    video_offset_seconds: Optional[float] = None
    actor_ids: Optional[List[int]] = Field(default=None, min_length=1)


class FeedbackActorOut(BaseModel):
    actor_id: int
    name: str


class FeedbackOut(BaseModel):
    feedback_id: int
    session_id: int
    content: str
    video_offset_seconds: Optional[float]
    created_at: str
    actors: List[FeedbackActorOut]

    class Config:
        from_attributes = True


# ── 헬퍼 ────────────────────────────────────────────────

async def _get_session_project_id(db: AsyncSession, session_id: int) -> int:
    project_id = (
        await db.execute(
            select(Session.project_id).where(Session.session_id == session_id)
        )
    ).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")
    return project_id


async def _load_project_actors(
    db: AsyncSession, project_id: int, actor_ids: List[int]
) -> List[Actor]:
    """중복 제거 → 같은 프로젝트 소속인지 검증 → Actor 리스트 반환"""
    unique_ids = list(dict.fromkeys(actor_ids))
    result = await db.execute(
        select(Actor)
        .where(Actor.project_id == project_id, Actor.actor_id.in_(unique_ids))
        .order_by(Actor.actor_id)
    )
    actors = result.scalars().all()
    found = {a.actor_id for a in actors}
    missing = [aid for aid in unique_ids if aid not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"세션의 프로젝트에 속하지 않은 배우입니다: {missing}",
        )
    return actors


async def _load_actors_by_feedback(
    db: AsyncSession, feedback_ids: List[int]
) -> Dict[int, List[Actor]]:
    if not feedback_ids:
        return {}
    result = await db.execute(
        select(FeedbackActorMention.feedback_id, Actor)
        .join(Actor, Actor.actor_id == FeedbackActorMention.actor_id)
        .where(FeedbackActorMention.feedback_id.in_(feedback_ids))
        .order_by(FeedbackActorMention.feedback_id, Actor.actor_id)
    )
    grouped: Dict[int, List[Actor]] = {}
    for fid, actor in result.all():
        grouped.setdefault(fid, []).append(actor)
    return grouped


def _to_out(feedback: Feedback, actors: List[Actor]) -> FeedbackOut:
    return FeedbackOut(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        created_at=feedback.created_at.isoformat(),
        actors=[
            FeedbackActorOut(
                actor_id=a.actor_id,
                name=a.name or f"배우 {a.actor_id}",
            )
            for a in actors
        ],
    )


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("", response_model=FeedbackOut, status_code=201)
async def create_feedback(
    session_id: int,
    body: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
):
    """피드백 작성 — 배우 1명 이상 mention 필수"""
    project_id = await _get_session_project_id(db, session_id)
    actors = await _load_project_actors(db, project_id, body.actor_ids)

    feedback = Feedback(
        session_id=session_id,
        content=body.content,
        video_offset_seconds=body.video_offset_seconds,
    )
    db.add(feedback)
    await db.flush()

    db.add_all([
        FeedbackActorMention(feedback_id=feedback.feedback_id, actor_id=a.actor_id)
        for a in actors
    ])
    await db.flush()

    return _to_out(feedback, actors)


@router.get("", response_model=List[FeedbackOut])
async def get_feedbacks(
    session_id: int,
    actor_id: Optional[int] = Query(None, description="특정 배우가 mention된 피드백만 필터"),
    db: AsyncSession = Depends(get_db),
):
    """세션의 모든 피드백 조회. actor_id 지정 시 해당 배우 mention만 필터링."""
    stmt = (
        select(Feedback)
        .where(Feedback.session_id == session_id)
        .order_by(Feedback.created_at)
    )
    if actor_id is not None:
        stmt = stmt.join(FeedbackActorMention).where(
            FeedbackActorMention.actor_id == actor_id
        )

    feedbacks = (await db.execute(stmt)).scalars().all()
    actors_by_fid = await _load_actors_by_feedback(
        db, [f.feedback_id for f in feedbacks]
    )
    return [_to_out(f, actors_by_fid.get(f.feedback_id, [])) for f in feedbacks]


@router.patch("/{feedback_id}", response_model=FeedbackOut)
async def update_feedback(
    session_id: int,
    feedback_id: int,
    body: FeedbackUpdate,
    db: AsyncSession = Depends(get_db),
):
    """피드백 수정 — actor_ids 제공 시 mention 집합을 통째로 교체"""
    feedback = (
        await db.execute(
            select(Feedback).where(
                Feedback.feedback_id == feedback_id,
                Feedback.session_id == session_id,
            )
        )
    ).scalar_one_or_none()
    if not feedback:
        raise HTTPException(status_code=404, detail="피드백을 찾을 수 없어요")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="수정할 내용이 없어요")

    new_actor_ids = update_data.pop("actor_ids", None)
    for key, value in update_data.items():
        setattr(feedback, key, value)

    actors: Optional[List[Actor]] = None
    if new_actor_ids is not None:
        project_id = await _get_session_project_id(db, session_id)
        actors = await _load_project_actors(db, project_id, new_actor_ids)
        await db.execute(
            delete(FeedbackActorMention).where(
                FeedbackActorMention.feedback_id == feedback.feedback_id
            )
        )
        db.add_all([
            FeedbackActorMention(feedback_id=feedback.feedback_id, actor_id=a.actor_id)
            for a in actors
        ])

    await db.flush()

    if actors is None:
        actors_by_fid = await _load_actors_by_feedback(db, [feedback.feedback_id])
        actors = actors_by_fid.get(feedback.feedback_id, [])

    return _to_out(feedback, actors)


@router.delete("/{feedback_id}", status_code=204)
async def delete_feedback(
    session_id: int,
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
):
    """피드백 삭제 (mention은 cascade로 함께 삭제)"""
    feedback = (
        await db.execute(
            select(Feedback).where(
                Feedback.feedback_id == feedback_id,
                Feedback.session_id == session_id,
            )
        )
    ).scalar_one_or_none()
    if not feedback:
        raise HTTPException(status_code=404, detail="피드백을 찾을 수 없어요")

    await db.delete(feedback)
