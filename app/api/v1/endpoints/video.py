"""
video.py
────────
영상 업로드 (S3 multipart) 및 face-analysis 트리거.

업로드 흐름:
  1) POST .../video/upload/init       → upload_id + s3_key 발급
  2) POST .../video/upload/part-urls  → 파트별 presigned PUT URL 발급
     (PWA가 이 URL들로 S3에 직접 PUT, 서버는 바이트를 보지 않음)
  3) POST .../video/upload/complete   → S3 조립 + Video row 업데이트 + analyzer 트리거
  실패/취소 시: POST .../video/upload/abort
"""

import json
import math
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.core.config import settings
from app.core.face_analyzer import request_face_analysis
from app.core.s3 import (
    abort_multipart_upload,
    complete_multipart_upload,
    create_multipart_upload,
    generate_part_upload_url,
    s3_object_url,
)
from app.models.models import Video, Session

router = APIRouter(prefix="/sessions/{session_id}/video", tags=["video"])
analysis_router = APIRouter(prefix="/videos", tags=["video"])

ALLOWED_MIME = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}


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


# ─────────────────────────────────────────────────────────────────────────────
# Multipart upload
# ─────────────────────────────────────────────────────────────────────────────

class InitUploadRequest(BaseModel):
    content_type: str = Field(..., description="video/webm | video/mp4 | video/quicktime")
    file_size: int = Field(..., gt=0, description="전체 파일 크기(bytes)")

    @field_validator("content_type")
    @classmethod
    def _check_mime(cls, v: str) -> str:
        if v not in ALLOWED_MIME:
            raise ValueError("지원하지 않는 영상 형식이에요")
        return v


class InitUploadResponse(BaseModel):
    upload_id: str
    s3_key: str
    video_id: int
    part_size: int
    part_count: int


class PartUrlsRequest(BaseModel):
    upload_id: str
    s3_key: str
    part_numbers: list[int] = Field(..., min_length=1)


class PartUrlsResponse(BaseModel):
    urls: dict[int, str]
    expires_in: int


class CompletedPart(BaseModel):
    part_number: int = Field(..., ge=1)
    etag: str


class CompleteUploadRequest(BaseModel):
    upload_id: str
    s3_key: str
    parts: list[CompletedPart] = Field(..., min_length=1)


class AbortUploadRequest(BaseModel):
    upload_id: str
    s3_key: str


@router.post("/upload/init", response_model=InitUploadResponse)
async def init_video_upload(
    session_id: int,
    payload: InitUploadRequest,
    db: AsyncSession = Depends(get_db),
):
    """multipart 업로드 시작. Video row 생성/초기화."""
    max_bytes = settings.MAX_VIDEO_SIZE_MB * 1024 * 1024
    if payload.file_size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"영상이 {settings.MAX_VIDEO_SIZE_MB}MB를 초과해요",
        )

    part_size = settings.UPLOAD_PART_SIZE_MB * 1024 * 1024
    part_count = math.ceil(payload.file_size / part_size)
    if part_count > settings.UPLOAD_MAX_PARTS:
        raise HTTPException(
            status_code=413,
            detail=f"파트 수가 {settings.UPLOAD_MAX_PARTS}개를 초과해요",
        )

    ext = ALLOWED_MIME[payload.content_type]
    s3_key = f"videos/{session_id}/{uuid.uuid4().hex}.{ext}"
    upload_id = await create_multipart_upload(s3_key, payload.content_type)

    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if video:
        video.s3_key = s3_key
        video.s3_url = None
        video.analysis_status = "uploading"
        video.analysis_result = None
        video.record_started_at = video.record_started_at or datetime.utcnow()
        video.record_ended_at = None
    else:
        video = Video(
            session_id=session_id,
            s3_key=s3_key,
            s3_url=None,
            analysis_status="uploading",
            record_started_at=datetime.utcnow(),
        )
        db.add(video)

    await db.flush()
    await db.commit()

    return InitUploadResponse(
        upload_id=upload_id,
        s3_key=s3_key,
        video_id=video.video_id,
        part_size=part_size,
        part_count=part_count,
    )


@router.post("/upload/part-urls", response_model=PartUrlsResponse)
async def get_upload_part_urls(
    session_id: int,
    payload: PartUrlsRequest,
):
    """파트별 presigned PUT URL 발급. 클라이언트가 이 URL로 S3에 직접 PUT."""
    # 한 번에 너무 많이 발급 못 하게 제한
    if len(payload.part_numbers) > settings.UPLOAD_MAX_PARTS:
        raise HTTPException(status_code=400, detail="요청 파트 수가 너무 많아요")

    if not payload.s3_key.startswith(f"videos/{session_id}/"):
        raise HTTPException(status_code=400, detail="잘못된 s3_key")

    urls: dict[int, str] = {}
    for n in payload.part_numbers:
        if n < 1 or n > settings.UPLOAD_MAX_PARTS:
            raise HTTPException(status_code=400, detail=f"잘못된 part_number: {n}")
        urls[n] = await generate_part_upload_url(
            payload.s3_key,
            payload.upload_id,
            n,
            expires_in=settings.UPLOAD_URL_EXPIRES_SECONDS,
        )

    return PartUrlsResponse(urls=urls, expires_in=settings.UPLOAD_URL_EXPIRES_SECONDS)


@router.post("/upload/complete")
async def complete_video_upload(
    session_id: int,
    payload: CompleteUploadRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """모든 파트 업로드 후 S3에 조립 요청. analyzer 트리거."""
    if not payload.s3_key.startswith(f"videos/{session_id}/"):
        raise HTTPException(status_code=400, detail="잘못된 s3_key")

    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if not video or video.s3_key != payload.s3_key:
        raise HTTPException(status_code=404, detail="업로드 세션을 찾을 수 없어요")

    parts_payload = [
        {"PartNumber": p.part_number, "ETag": p.etag}
        for p in payload.parts
    ]
    s3_url = await complete_multipart_upload(payload.s3_key, payload.upload_id, parts_payload)

    video.s3_url = s3_url
    video.record_ended_at = datetime.utcnow()
    video.analysis_status = "pending"
    video.analysis_result = None

    sess_result = await db.execute(select(Session).where(Session.session_id == session_id))
    sess = sess_result.scalar_one_or_none()
    if sess:
        sess.in_progress = False

    await db.flush()
    await db.commit()

    background_tasks.add_task(
        request_face_analysis,
        video_id=video.video_id,
        session_id=session_id,
        s3_key=payload.s3_key,
        s3_url=s3_url,
    )

    return {
        "video_id": video.video_id,
        "s3_url": s3_url,
        "analysis_status": "pending",
    }


@router.post("/upload/abort")
async def abort_video_upload(
    session_id: int,
    payload: AbortUploadRequest,
    db: AsyncSession = Depends(get_db),
):
    """업로드 취소. S3 조각 정리 + Video row 정리."""
    if not payload.s3_key.startswith(f"videos/{session_id}/"):
        raise HTTPException(status_code=400, detail="잘못된 s3_key")

    await abort_multipart_upload(payload.s3_key, payload.upload_id)

    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if video and video.s3_key == payload.s3_key and video.analysis_status == "uploading":
        video.s3_key = None
        video.analysis_status = "pending"
        await db.flush()
        await db.commit()

    return {"aborted": True}


# ─────────────────────────────────────────────────────────────────────────────
# 조회 / 재분석 / 콜백
# ─────────────────────────────────────────────────────────────────────────────

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
