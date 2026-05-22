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
from app.models.models import Actor, Feedback, FeedbackActorMention, Session, FeedbackTag
from app.services.feedback_classify import classify_unclassified, classify_one

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

 class FeedbackWithTags(BaseModel):
    feedback_id: int
    session_id: int
    content: str
    video_offset_seconds: Optional[float]
    created_at: str
    priority: List[str]      # 예: ["required"] 또는 ["praise", "discussion"]
    categories: List[str]    # 예: ["acting:tone", "acting:expression"]


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


@router.get("/with-tags", response_model=List[FeedbackWithTags])
async def get_feedbacks_with_tags(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """세션의 모든 피드백 + 태그 함께 조회 (프론트 목록용)"""
    # 피드백 전부 가져오기
    result = await db.execute(
        select(Feedback)
        .where(Feedback.session_id == session_id)
        .order_by(Feedback.created_at)
    )
    feedbacks = result.scalars().all()

    if not feedbacks:
        return []

    # 이 세션 피드백들의 태그를 한 번에 가져오기 (N+1 방지)
    feedback_ids = [f.feedback_id for f in feedbacks]
    tag_result = await db.execute(
        select(FeedbackTag).where(FeedbackTag.feedback_id.in_(feedback_ids))
    )
    all_tags = tag_result.scalars().all()

    # feedback_id별로 태그 묶기
    tags_by_feedback: dict[int, list] = {}
    for tag in all_tags:
        tags_by_feedback.setdefault(tag.feedback_id, []).append(tag)

    # 응답 조립
    output = []
    for fb in feedbacks:
        fb_tags = tags_by_feedback.get(fb.feedback_id, [])
        output.append(FeedbackWithTags(
            feedback_id=fb.feedback_id,
            session_id=fb.session_id,
            content=fb.content,
            video_offset_seconds=fb.video_offset_seconds,
            created_at=fb.created_at.isoformat(),
            priority=[t.tag_value for t in fb_tags if t.tag_type == "priority"],
            categories=[t.tag_value for t in fb_tags if t.tag_type == "category"],
        ))
    return output

@router.get("/filter", response_model=List[FeedbackWithTags])
async def filter_feedbacks(
    session_id: int,
    priority: Optional[str] = None,   # required | recommended | discussion | praise
    category: Optional[str] = None,   # 예: acting:tone, vocal:pitch
    db: AsyncSession = Depends(get_db),
):
    """
    우선순위·카테고리로 피드백 필터링 (둘 다 선택적)
    - priority만: 해당 우선순위 피드백
    - category만: 해당 카테고리 피드백
    - 둘 다: 두 조건 모두 만족하는 피드백 (AND)
    - 둘 다 없음: 세션의 모든 피드백
    """
    # ① 조건에 맞는 feedback_id 후보 수집
    matching_ids: Optional[set[int]] = None

    if priority is not None:
        res = await db.execute(
            select(FeedbackTag.feedback_id)
            .where(FeedbackTag.tag_type == "priority")
            .where(FeedbackTag.tag_value == priority)
        )
        priority_ids = {r[0] for r in res.all()}
        matching_ids = priority_ids

    if category is not None:
        res = await db.execute(
            select(FeedbackTag.feedback_id)
            .where(FeedbackTag.tag_type == "category")
            .where(FeedbackTag.tag_value == category)
        )
        category_ids = {r[0] for r in res.all()}
        # priority도 있으면 교집합(AND), 아니면 그대로
        matching_ids = (
            matching_ids & category_ids if matching_ids is not None else category_ids
        )

    # ② 피드백 조회
    query = select(Feedback).where(Feedback.session_id == session_id)
    if matching_ids is not None:
        if not matching_ids:
            return []  # 조건 맞는 게 없음
        query = query.where(Feedback.feedback_id.in_(matching_ids))
    query = query.order_by(Feedback.created_at)

    result = await db.execute(query)
    feedbacks = result.scalars().all()
    if not feedbacks:
        return []

    # ③ 태그 붙여서 반환 (with-tags랑 동일 형식)
    feedback_ids = [f.feedback_id for f in feedbacks]
    tag_result = await db.execute(
        select(FeedbackTag).where(FeedbackTag.feedback_id.in_(feedback_ids))
    )
    all_tags = tag_result.scalars().all()

    tags_by_feedback: dict[int, list] = {}
    for tag in all_tags:
        tags_by_feedback.setdefault(tag.feedback_id, []).append(tag)

    output = []
    for fb in feedbacks:
        fb_tags = tags_by_feedback.get(fb.feedback_id, [])
        output.append(FeedbackWithTags(
            feedback_id=fb.feedback_id,
            session_id=fb.session_id,
            content=fb.content,
            video_offset_seconds=fb.video_offset_seconds,
            created_at=fb.created_at.isoformat(),
            priority=[t.tag_value for t in fb_tags if t.tag_type == "priority"],
            categories=[t.tag_value for t in fb_tags if t.tag_type == "category"],
        ))
    return output

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

@router.post("/classify")
async def classify_session_feedbacks(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """이 세션의 미분류 피드백을 일괄 분류"""
    return await classify_unclassified(db, session_id=session_id, limit=50)


@router.post("/{feedback_id}/classify")
async def classify_single(
    session_id: int,
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
):
    """단일 피드백 분류 (재분류 가능)"""
    return await classify_one(db, feedback_id)

# ── 태그 포함 조회 엔드포인트 ──────────────────────────────

@router.get("/{feedback_id}/tags")
async def get_feedback_tags(
    session_id: int,
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
):
    """단일 피드백의 태그만 조회"""
    result = await db.execute(
        select(FeedbackTag).where(FeedbackTag.feedback_id == feedback_id)
    )
    tags = result.scalars().all()
    return {
        "feedback_id": feedback_id,
        "priority": [t.tag_value for t in tags if t.tag_type == "priority"],
        "categories": [t.tag_value for t in tags if t.tag_type == "category"],
    }
