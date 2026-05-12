"""
video.py
────────
영상 업로드 (S3) 및 face-analysis 트리거
"""

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.config import settings
from app.core.face_analyzer import request_face_analysis
from app.core.s3 import upload_video
from app.models.models import Video, Session

router = APIRouter(prefix="/sessions/{session_id}/video", tags=["video"])
analysis_router = APIRouter(prefix="/videos", tags=["video"])

ALLOWED_MIME = {"video/mp4", "video/quicktime", "video/webm", "application/octet-stream"}


class AnalysisCallbackPayload(BaseModel):
    video_id: int
    analysis_status: str
    analysis_result: dict[str, Any] | list[Any] | str | None = None
    error_message: str | None = None


def _decode_analysis_result(raw_result: str | None) -> Any:
    if raw_result is None:
        return None
    try:
        return json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result


@router.post("/upload")
async def upload_video_file(
    session_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """핸드폰에서 촬영한 영상 S3에 업로드"""
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail="지원하지 않는 영상 형식이에요")

    # 파일 크기 제한
    content = await file.read()
    max_bytes = settings.MAX_VIDEO_SIZE_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"영상이 {settings.MAX_VIDEO_SIZE_MB}MB를 초과해요")

    # S3 업로드
    s3_key = f"videos/{session_id}/{uuid.uuid4().hex}.webm"
    s3_url = await upload_video(content, s3_key, file.content_type or "video/webm")

    # DB 저장
    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()

    if video:
        video.s3_key = s3_key
        video.s3_url = s3_url
        video.record_ended_at = datetime.utcnow()
        video.analysis_status = "pending"
        video.analysis_result = None
    else:
        video = Video(
            session_id=session_id,
            s3_key=s3_key,
            s3_url=s3_url,
            record_ended_at=datetime.utcnow(),
            analysis_status="pending",
        )
        db.add(video)

    # 세션 in_progress 종료
    sess_result = await db.execute(select(Session).where(Session.session_id == session_id))
    session = sess_result.scalar_one_or_none()
    if session:
        session.in_progress = False

    await db.flush()
    await db.commit()
    background_tasks.add_task(
        request_face_analysis,
        video_id=video.video_id,
        session_id=session_id,
        s3_key=s3_key,
        s3_url=s3_url,
    )

    return {
        "video_id": video.video_id,
        "s3_url": s3_url,
        "analysis_status": "pending",
    }


@router.get("")
async def get_video(
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """세션의 영상 정보 조회"""
    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")

    return {
        "video_id": video.video_id,
        "s3_url": video.s3_url,
        "analysis_status": video.analysis_status,
        "analysis_result": _decode_analysis_result(video.analysis_result),
    }


@router.post("/analyze")
async def analyze_existing_video(
    session_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """이미 업로드된 세션 영상을 외부 face-analyzer 서비스로 분석 요청."""
    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")
    if not video.s3_key or not video.s3_url:
        raise HTTPException(status_code=400, detail="분석할 S3 영상 정보가 없어요")

    video.analysis_status = "pending"
    video.analysis_result = None
    await db.flush()
    await db.commit()

    background_tasks.add_task(
        request_face_analysis,
        video_id=video.video_id,
        session_id=session_id,
        s3_key=video.s3_key,
        s3_url=video.s3_url,
    )

    return {
        "video_id": video.video_id,
        "s3_url": video.s3_url,
        "analysis_status": video.analysis_status,
    }


@analysis_router.post("/analysis-callback")
async def update_analysis_result(
    payload: AnalysisCallbackPayload,
    x_analyzer_secret: str | None = Header(default=None, alias="X-Analyzer-Secret"),
    db: AsyncSession = Depends(get_db),
):
    """외부 face-analyzer 서비스가 분석 완료/실패 결과를 저장하는 callback."""
    if settings.FACE_ANALYZER_SECRET and x_analyzer_secret != settings.FACE_ANALYZER_SECRET:
        raise HTTPException(status_code=401, detail="invalid analyzer secret")

    result = await db.execute(select(Video).where(Video.video_id == payload.video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")

    if payload.analysis_status not in {"pending", "processing", "done", "failed"}:
        raise HTTPException(status_code=400, detail="invalid analysis_status")

    video.analysis_status = payload.analysis_status
    if payload.analysis_result is not None:
        video.analysis_result = json.dumps(payload.analysis_result, ensure_ascii=False)
    elif payload.error_message is not None:
        video.analysis_result = json.dumps({"error_message": payload.error_message}, ensure_ascii=False)

    return {
        "video_id": video.video_id,
        "analysis_status": video.analysis_status,
    }
