"""
feedback.py
───────────
피드백 작성, 조회, 수정, 삭제
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Feedback, FeedbackTag

from app.services.feedback_classify import classify_unclassified, classify_one


router = APIRouter(prefix="/sessions/{session_id}/feedbacks", tags=["feedback"])


# ── 스키마 ──────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    content: str
    video_offset_seconds: Optional[float] = None


class FeedbackUpdate(BaseModel):
    content: Optional[str] = None
    video_offset_seconds: Optional[float] = None


class FeedbackOut(BaseModel):
    feedback_id: int
    session_id: int
    content: str
    video_offset_seconds: Optional[float]
    created_at: str

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


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("", response_model=FeedbackOut, status_code=201)
async def create_feedback(
    session_id: int,
    body: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
):
    """피드백 작성 (영상 촬영 중 실시간 작성 가능)"""
    feedback = Feedback(
        session_id=session_id,
        content=body.content,
        video_offset_seconds=body.video_offset_seconds,
    )
    db.add(feedback)
    await db.flush()

    return FeedbackOut(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        created_at=feedback.created_at.isoformat(),
    )


@router.get("", response_model=List[FeedbackOut])
async def get_feedbacks(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """세션의 모든 피드백 조회"""
    result = await db.execute(
        select(Feedback)
        .where(Feedback.session_id == session_id)
        .order_by(Feedback.created_at)
    )
    feedbacks = result.scalars().all()

    return [
        FeedbackOut(
            feedback_id=f.feedback_id,
            session_id=f.session_id,
            content=f.content,
            video_offset_seconds=f.video_offset_seconds,
            created_at=f.created_at.isoformat(),
        )
        for f in feedbacks
    ]


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
    """피드백 수정 (content / video_offset_seconds 부분 수정 가능)"""
    result = await db.execute(
        select(Feedback).where(
            Feedback.feedback_id == feedback_id,
            Feedback.session_id == session_id,
        )
    )
    feedback = result.scalar_one_or_none()
    if not feedback:
        raise HTTPException(status_code=404, detail="피드백을 찾을 수 없어요")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="수정할 내용이 없어요")

    for key, value in update_data.items():
        setattr(feedback, key, value)

    await db.flush()
    await db.refresh(feedback)

    return FeedbackOut(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        created_at=feedback.created_at.isoformat(),
    )


@router.delete("/{feedback_id}", status_code=204)
async def delete_feedback(
    session_id: int,
    feedback_id: int,
    db: AsyncSession = Depends(get_db),
):
    """피드백 삭제"""
    result = await db.execute(
        select(Feedback).where(
            Feedback.feedback_id == feedback_id,
            Feedback.session_id == session_id,
        )
    )
    feedback = result.scalar_one_or_none()
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