import logging
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _callback_url() -> str | None:
    if not settings.PUBLIC_API_BASE_URL:
        return None
    return f"{settings.PUBLIC_API_BASE_URL.rstrip('/')}/api/v1/videos/analysis-callback"


async def request_face_analysis(
    *,
    video_id: int,
    session_id: int,
    s3_key: str,
    s3_url: str,
    known_actors: list[dict[str, Any]] | None = None,
    thumbnail_dir: str | None = None,
) -> bool:
    """외부 face-analyzer 서비스에 분석 요청.

    known_actors: 같은 프로젝트의 기존 배우 리스트 — analyzer가 ActorMatcher로
    유사도 비교 후 matched / new_candidates 로 분기해 콜백.
    형식: [{"actor_id": int, "embedding": list[float]}, ...]

    thumbnail_dir: analyzer가 썸네일을 S3에 PUT할 디렉터리 (끝에 슬래시 포함).
    예: "{project_id}/{session_id}/" → analyzer는 이 안에 "thumb-{idx}.jpg" 형식으로 저장.
    """
    if not settings.FACE_ANALYZER_URL:
        logger.info("FACE_ANALYZER_URL is not configured; skipping analysis request")
        return False

    callback_url = _callback_url()
    if not callback_url:
        logger.warning("PUBLIC_API_BASE_URL is not configured; skipping analysis request")
        return False

    payload = {
        "video_id": video_id,
        "session_id": session_id,
        "s3_key": s3_key,
        "s3_url": s3_url,
        "callback_url": callback_url,
        "known_actors": known_actors or [],
        "thumbnail_dir": thumbnail_dir,
    }
    headers = {}
    if settings.FACE_ANALYZER_SECRET:
        headers["X-Analyzer-Secret"] = settings.FACE_ANALYZER_SECRET

    analyze_url = f"{settings.FACE_ANALYZER_URL.rstrip('/')}/analyze"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(analyze_url, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to request face analysis for video_id=%s", video_id)
        return False

    return True
