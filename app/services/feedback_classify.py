import logging
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Feedback, FeedbackTag
from app.services.classifier import classify_feedback, extract_tags

logger = logging.getLogger(__name__)


async def classify_unclassified(
    db: AsyncSession,
    session_id: int | None = None,   # ← 추가: 특정 세션만 분류 (None이면 전체)
    limit: int = 50,
) -> dict:
    """태그가 아직 없는 피드백을 일괄 분류"""
    subq = select(FeedbackTag.feedback_id).distinct()
    query = select(Feedback).where(Feedback.feedback_id.notin_(subq))

    if session_id is not None:
        query = query.where(Feedback.session_id == session_id)

    result = await db.execute(query.limit(limit))
    feedbacks = result.scalars().all()

    if not feedbacks:
        logger.info("분류할 피드백 없음")
        return {"processed": 0, "success": 0, "failed": 0}

    success, failed = 0, 0

    for fb in feedbacks:
        if not fb.content or not fb.content.strip():
            continue
        try:
            classification = await classify_feedback(fb.content)
            tags = extract_tags(classification)
            for t in tags:
                db.add(FeedbackTag(
                    feedback_id=fb.feedback_id,
                    tag_type=t["tag_type"],
                    tag_value=t["tag_value"],
                ))
            logger.info(f"✅ [{fb.feedback_id}] {fb.content[:30]} → "
                        f"{', '.join(t['tag_value'] for t in tags)}")
            success += 1
        except Exception as e:
            logger.error(f"❌ [{fb.feedback_id}] 실패: {e}")
            failed += 1

    await db.commit()
    logger.info(f"완료! 성공 {success} / 실패 {failed}")
    return {"processed": len(feedbacks), "success": success, "failed": failed}


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