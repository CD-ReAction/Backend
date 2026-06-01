import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Feedback, FeedbackTag
from app.services.classifier import classify_feedback, extract_tags

logger = logging.getLogger(__name__)


async def classify_one(db: AsyncSession, feedback_id: int) -> dict:
    """단일 피드백 분류 (재분류 가능)"""
    result = await db.execute(
        select(Feedback).where(Feedback.feedback_id == feedback_id)
    )
    fb = result.scalar_one_or_none()
    if fb is None:
        return {"error": "피드백 없음"}

    classification = await classify_feedback(fb.content)
    tags = extract_tags(classification)

    await db.execute(
        delete(FeedbackTag).where(FeedbackTag.feedback_id == feedback_id)
    )
    for t in tags:
        db.add(FeedbackTag(
            feedback_id=feedback_id,
            tag_type=t["tag_type"],
            tag_value=t["tag_value"],
        ))
    await db.commit()

    return {
        "feedback_id": feedback_id,
        "tags": [t["tag_value"] for t in tags],
        "classification": classification,
    }

async def classify_unclassified(
    db: AsyncSession,
    session_id: int | None = None,
    limit: int = 50,
    force: bool = False,   # ← 추가
) -> dict:
    # force=True면 기존 태그 여부 무관하게 전체 가져옴
    if not force:
        subq = select(FeedbackTag.feedback_id).distinct()
        query = select(Feedback).where(Feedback.feedback_id.notin_(subq))
    else:
        query = select(Feedback)

    if session_id is not None:
        query = query.where(Feedback.session_id == session_id)

    result = await db.execute(query.limit(limit))
    feedbacks = result.scalars().all()

    if not feedbacks:
        return {"processed": 0, "success": 0, "failed": 0}

    success, failed = 0, 0

    for fb in feedbacks:
        if not fb.content or not fb.content.strip():
            continue
        try:
            # force면 기존 태그 먼저 삭제
            if force:
                await db.execute(
                    delete(FeedbackTag).where(
                        FeedbackTag.feedback_id == fb.feedback_id
                    )
                )
            classification = await classify_feedback(fb.content)
            tags = extract_tags(classification)
            for t in tags:
                db.add(FeedbackTag(
                    feedback_id=fb.feedback_id,
                    tag_type=t["tag_type"],
                    tag_value=t["tag_value"],
                ))
            success += 1
        except Exception as e:
            logger.error(f"❌ [{fb.feedback_id}] 실패: {e}")
            failed += 1

    await db.commit()
    return {"processed": len(feedbacks), "success": success, "failed": failed}