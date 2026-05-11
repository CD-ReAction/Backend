"""
feedback.py
───────────
피드백 작성 및 조회
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import Feedback

router = APIRouter(prefix="/sessions/{session_id}/feedbacks", tags=["feedback"])


# ── 스키마 ──────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    content: str
    video_offset_seconds: Optional[float] = None  # 영상 타임스탬프


class FeedbackOut(BaseModel):
    feedback_id: int
    session_id: int
    content: str
    video_offset_seconds: Optional[float]
    created_at: str

    class Config:
        from_attributes = True


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