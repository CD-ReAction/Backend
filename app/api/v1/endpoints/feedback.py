"""
feedback.py
───────────
피드백 작성, 조회, 수정, 삭제
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Actor, Feedback, FeedbackActor, FeedbackTag

from app.services.feedback_classify import classify_unclassified, classify_one


router = APIRouter(prefix="/sessions/{session_id}/feedbacks", tags=["feedback"])


# ── 스키마 ──────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    content: str
    video_offset_seconds: Optional[float] = None
    actor_ids: List[int] = []   # 피드백 대상 배우(여러 명 가능)


class FeedbackUpdate(BaseModel):
    content: Optional[str] = None
    video_offset_seconds: Optional[float] = None
    actor_ids: Optional[List[int]] = None   # 지정 시 전체 교체


class FeedbackOut(BaseModel):
    feedback_id: int
    session_id: int
    created_by_user_id: int
    content: str
    video_offset_seconds: Optional[float]
    actor_ids: List[int]
    created_at: str

    class Config:
        from_attributes = True


class FeedbackWithTags(BaseModel):
    feedback_id: int
    session_id: int
    created_by_user_id: int
    content: str
    video_offset_seconds: Optional[float]
    actor_ids: List[int]
    created_at: str
    priority: List[str]      # 예: ["required"] 또는 ["praise", "discussion"]
    categories: List[str]    # 예: ["acting:tone", "acting:expression"]


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("", response_model=FeedbackOut, status_code=201)
async def create_feedback(
    session_id: int,
    body: FeedbackCreate,
    user_id: int = Query(..., description="작성자 user_id"),
    db: AsyncSession = Depends(get_db),
):
    """피드백 작성 (영상 촬영 중 실시간 작성 가능). actor_ids로 대상 배우 여러 명 지정 가능"""
    feedback = Feedback(
        session_id=session_id,
        created_by_user_id=user_id,
        content=body.content,
        video_offset_seconds=body.video_offset_seconds,
    )
    db.add(feedback)
    await db.flush()

    actor_ids = list(dict.fromkeys(body.actor_ids))  # 중복 제거, 순서 유지
    if actor_ids:
        res = await db.execute(
            select(Actor.actor_id).where(Actor.actor_id.in_(actor_ids))
        )
        existing = {r[0] for r in res.all()}
        missing = [aid for aid in actor_ids if aid not in existing]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"존재하지 않는 배우: {missing}",
            )
        for aid in actor_ids:
            db.add(FeedbackActor(feedback_id=feedback.feedback_id, actor_id=aid))
        await db.flush()

    return FeedbackOut(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        created_by_user_id=feedback.created_by_user_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        actor_ids=actor_ids,
        created_at=feedback.created_at.isoformat(),
    )


@router.get("", response_model=List[FeedbackOut])
async def get_feedbacks(
    session_id: int,
    actor_ids: List[int] = Query(default=[]),  # 여러 개 가능: ?actor_ids=1&actor_ids=2
    user_id: Optional[int] = Query(default=None, description="지정 시 해당 사용자가 작성한 피드백만 반환"),
    db: AsyncSession = Depends(get_db),
):
    """세션의 피드백 조회. actor_ids 지정 시 해당 배우 중 한 명이라도 연결된 피드백만 (OR).
    user_id 지정 시 해당 사용자가 작성한 피드백만 (작성 화면용). 미지정 시 세션의 모든 피드백 (다시보기용)."""
    query = select(Feedback).where(Feedback.session_id == session_id)
    if actor_ids:
        sub = (
            select(FeedbackActor.feedback_id)
            .where(FeedbackActor.actor_id.in_(actor_ids))
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
        FeedbackOut(
            feedback_id=f.feedback_id,
            session_id=f.session_id,
            created_by_user_id=f.created_by_user_id,
            content=f.content,
            video_offset_seconds=f.video_offset_seconds,
            actor_ids=actors_by_feedback.get(f.feedback_id, []),
            created_at=f.created_at.isoformat(),
        )
        for f in feedbacks
    ]


# [DEPRECATED] /filter 가 동일 응답을 더 일반적인 필터로 제공하므로 비활성화.
# 프론트 마이그레이션 완료 시 블록째 삭제.
# @router.get("/with-tags", response_model=List[FeedbackWithTags])
# async def get_feedbacks_with_tags(
#     session_id: int,
#     actor_ids: List[int] = Query(default=[]),
#     db: AsyncSession = Depends(get_db),
# ):
#     """세션의 피드백 + 태그 함께 조회 (프론트 목록용). actor_ids 지정 시 OR 필터"""
#     query = select(Feedback).where(Feedback.session_id == session_id)
#     if actor_ids:
#         sub = (
#             select(FeedbackActor.feedback_id)
#             .where(FeedbackActor.actor_id.in_(actor_ids))
#         )
#         query = query.where(Feedback.feedback_id.in_(sub))
#     query = query.order_by(Feedback.created_at)
#
#     result = await db.execute(query)
#     feedbacks = result.scalars().all()
#     if not feedbacks:
#         return []
#
#     feedback_ids = [f.feedback_id for f in feedbacks]
#     tag_result = await db.execute(
#         select(FeedbackTag).where(FeedbackTag.feedback_id.in_(feedback_ids))
#     )
#     all_tags = tag_result.scalars().all()
#
#     tags_by_feedback: dict[int, list] = {}
#     for tag in all_tags:
#         tags_by_feedback.setdefault(tag.feedback_id, []).append(tag)
#
#     actors_by_feedback = await _load_actor_ids(db, feedback_ids)
#
#     output = []
#     for fb in feedbacks:
#         fb_tags = tags_by_feedback.get(fb.feedback_id, [])
#         output.append(FeedbackWithTags(
#             feedback_id=fb.feedback_id,
#             session_id=fb.session_id,
#             content=fb.content,
#             video_offset_seconds=fb.video_offset_seconds,
#             actor_ids=actors_by_feedback.get(fb.feedback_id, []),
#             created_at=fb.created_at.isoformat(),
#             priority=[t.tag_value for t in fb_tags if t.tag_type == "priority"],
#             categories=[t.tag_value for t in fb_tags if t.tag_type == "category"],
#         ))
#     return output


async def _load_actor_ids(
    db: AsyncSession, feedback_ids: List[int]
) -> dict[int, list[int]]:
    """feedback_id 리스트에 매핑된 actor_id들을 한 번에 로드"""
    if not feedback_ids:
        return {}
    res = await db.execute(
        select(FeedbackActor.feedback_id, FeedbackActor.actor_id)
        .where(FeedbackActor.feedback_id.in_(feedback_ids))
    )
    out: dict[int, list[int]] = {}
    for fid, aid in res.all():
        out.setdefault(fid, []).append(aid)
    return out

@router.get("/filter", response_model=List[FeedbackWithTags])
async def filter_feedbacks(
    session_id: int,
    priority: Optional[str] = None,   # required | recommended | discussion | praise
    category: Optional[str] = None,   # 예: acting:tone, vocal:pitch
    actor_ids: List[int] = Query(default=[]),  # 여러 배우 중 한 명이라도 연결된 피드백 (OR)
    user_id: Optional[int] = Query(default=None, description="지정 시 해당 사용자가 작성한 피드백만 반환"),
    db: AsyncSession = Depends(get_db),
):
    """
    우선순위·카테고리·배우·작성자로 피드백 필터링 (전부 선택적)
    - priority/category 조건은 AND
    - actor_ids는 그 중 한 명이라도 매칭되면 포함 (OR)
    - user_id 지정 시 해당 사용자가 작성한 피드백만 (AND, 작성 화면용)
    - 여러 조건 함께 지정 시 전부 만족해야 함 (AND across types)
    - 아무 조건도 없으면: 세션의 모든 피드백
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
        matching_ids = (
            matching_ids & category_ids if matching_ids is not None else category_ids
        )

    if actor_ids:
        res = await db.execute(
            select(FeedbackActor.feedback_id)
            .where(FeedbackActor.actor_id.in_(actor_ids))
        )
        actor_match_ids = {r[0] for r in res.all()}
        matching_ids = (
            matching_ids & actor_match_ids if matching_ids is not None else actor_match_ids
        )

    # ② 피드백 조회
    query = select(Feedback).where(Feedback.session_id == session_id)
    if matching_ids is not None:
        if not matching_ids:
            return []
        query = query.where(Feedback.feedback_id.in_(matching_ids))
    if user_id is not None:
        query = query.where(Feedback.created_by_user_id == user_id)
    query = query.order_by(Feedback.created_at)

    result = await db.execute(query)
    feedbacks = result.scalars().all()
    if not feedbacks:
        return []

    # ③ 태그·배우 붙여서 반환
    feedback_ids = [f.feedback_id for f in feedbacks]
    tag_result = await db.execute(
        select(FeedbackTag).where(FeedbackTag.feedback_id.in_(feedback_ids))
    )
    all_tags = tag_result.scalars().all()

    tags_by_feedback: dict[int, list] = {}
    for tag in all_tags:
        tags_by_feedback.setdefault(tag.feedback_id, []).append(tag)

    actors_by_feedback = await _load_actor_ids(db, feedback_ids)

    output = []
    for fb in feedbacks:
        fb_tags = tags_by_feedback.get(fb.feedback_id, [])
        output.append(FeedbackWithTags(
            feedback_id=fb.feedback_id,
            session_id=fb.session_id,
            created_by_user_id=fb.created_by_user_id,
            content=fb.content,
            video_offset_seconds=fb.video_offset_seconds,
            actor_ids=actors_by_feedback.get(fb.feedback_id, []),
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
    user_id: int = Query(..., description="요청자 user_id (작성자 본인만 수정 가능)"),
    db: AsyncSession = Depends(get_db),
):
    """피드백 수정 (content / video_offset_seconds / actor_ids 부분 수정 가능).
    actor_ids 지정 시 기존 연결을 전체 교체. 빈 리스트면 전부 해제.
    작성자 본인이 아니면 403."""
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
        if new_actor_ids:
            res = await db.execute(
                select(Actor.actor_id).where(Actor.actor_id.in_(new_actor_ids))
            )
            existing = {r[0] for r in res.all()}
            missing = [aid for aid in new_actor_ids if aid not in existing]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"존재하지 않는 배우: {missing}",
                )

        # 기존 링크 전체 삭제 후 재삽입
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

    return FeedbackOut(
        feedback_id=feedback.feedback_id,
        session_id=feedback.session_id,
        created_by_user_id=feedback.created_by_user_id,
        content=feedback.content,
        video_offset_seconds=feedback.video_offset_seconds,
        actor_ids=actors_by_feedback.get(feedback_id, []),
        created_at=feedback.created_at.isoformat(),
    )


@router.delete("/{feedback_id}", status_code=204)
async def delete_feedback(
    session_id: int,
    feedback_id: int,
    user_id: int = Query(..., description="요청자 user_id (작성자 본인만 삭제 가능)"),
    db: AsyncSession = Depends(get_db),
):
    """피드백 삭제. 작성자 본인이 아니면 403."""
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
        raise HTTPException(status_code=403, detail="작성자만 삭제할 수 있어요")

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