"""
video.py
────────
영상 업로드 (S3) 및 face-analysis 트리거
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.config import settings
from app.core.s3 import upload_video
from app.models.models import Video, Session

router = APIRouter(prefix="/sessions/{session_id}/video", tags=["video"])

ALLOWED_MIME = {"video/mp4", "video/quicktime", "video/webm", "application/octet-stream"}


@router.post("/upload")
async def upload_video_file(
    session_id: int,
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
        "analysis_result": video.analysis_result,
    }