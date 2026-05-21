"""
video.py
────────
영상 업로드 (S3 multipart) 및 face-analysis 트리거/콜백.

업로드 흐름:
  1) POST .../video/upload/init       → upload_id + s3_key 발급
  2) POST .../video/upload/part-urls  → 파트별 presigned PUT URL 발급
     (PWA가 이 URL들로 S3에 직접 PUT, 서버는 바이트를 보지 않음)
  3) POST .../video/upload/complete   → S3 조립 + Video row 업데이트 + analyzer 트리거
  실패/취소 시: POST .../video/upload/abort

분석 콜백:
  POST /videos/analysis-callback
  - matched[]: analyzer가 known_actors 중 매칭한 항목 → VideoActor 링크만 추가
  - new_candidates[]: 새 얼굴 → Actor INSERT (name="배우 {actor_id}") + 링크
  - analysis_result.appearances[]: "new:{idx}" placeholder → "actor:{id}" 치환
"""

import json
import math
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, text

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
from app.models.models import Actor, Session, Video, VideoActor

router = APIRouter(prefix="/sessions/{session_id}/video", tags=["video"])
analysis_router = APIRouter(prefix="/videos", tags=["video"])

ALLOWED_MIME = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
}

# actor 갤러리 cap — 초과 시 oldest exemplar drop
GALLERY_CAP_PER_ACTOR = 20


def _cap_exemplars(exemplars: list[list[float]]) -> list[list[float]]:
    """초과 시 가장 오래된(앞쪽) exemplar 제거. append 순서 = 시간 순."""
    if len(exemplars) > GALLERY_CAP_PER_ACTOR:
        return exemplars[-GALLERY_CAP_PER_ACTOR:]
    return exemplars


# ─────────────────────────────────────────────────────────────────────────────
# 공용 헬퍼
# ─────────────────────────────────────────────────────────────────────────────

async def _load_known_actors(db: AsyncSession, project_id: int) -> list[dict[str, Any]]:
    """project의 모든 actor + 갤러리(다중 exemplar)를 analyzer payload 형태로 변환.

    face_embeddings는 list[list[float]] — analyzer는 각 actor에 대해
    여러 exemplar 중 가장 가까운 것을 매칭에 사용 (max-of-N).
    """
    result = await db.execute(
        select(Actor).where(Actor.project_id == project_id, Actor.face_embeddings.isnot(None))
    )
    actors = result.scalars().all()
    payload: list[dict[str, Any]] = []
    for a in actors:
        templates = a.face_embeddings or []
        if not templates:
            continue
        payload.append({"actor_id": a.actor_id, "face_templates": templates})
    return payload


async def _project_id_for_video(db: AsyncSession, video: Video) -> int | None:
    result = await db.execute(select(Session).where(Session.session_id == video.session_id))
    sess = result.scalar_one_or_none()
    return sess.project_id if sess else None


def _build_actor_response(actor: Actor, is_new: bool) -> dict[str, Any]:
    return {
        "actor_id": actor.actor_id,
        "name": actor.name or f"배우 {actor.actor_id}",
        "thumbnail_url": s3_object_url(actor.thumbnail_s3_key) if actor.thumbnail_s3_key else None,
        "is_new": is_new,
    }


def _decode_analysis_result(raw_result: str | None) -> Any:
    if raw_result is None:
        return None
    try:
        return json.loads(raw_result)
    except json.JSONDecodeError:
        return raw_result


# ─────────────────────────────────────────────────────────────────────────────
# Callback schema
# ─────────────────────────────────────────────────────────────────────────────

class MatchedActor(BaseModel):
    actor_id: int
    thumbnail_s3_key: str | None = None  # analyzer가 새로 찍은 키 (참고용, 저장 안 함)
    similarity: float | None = None
    # 이번 영상에서 새로 본 각도 — 기존 actor 갤러리에 누적
    new_exemplars: list[list[float]] = []


class NewCandidate(BaseModel):
    temp_index: int
    # analyzer가 S3 업로드 실패/미설정 시 None을 보낼 수 있어 nullable.
    # DB 컬럼/응답 빌더도 None 안전.
    thumbnail_s3_key: str | None = None
    # 다중 exemplar 갤러리 (analyzer가 within-video diversity 보장해서 5~10개 송신)
    face_embeddings: list[list[float]]


class AnalysisCallbackPayload(BaseModel):
    video_id: int
    analysis_status: str
    matched: list[MatchedActor] = []
    new_candidates: list[NewCandidate] = []
    analysis_result: dict[str, Any] | list[Any] | str | None = None
    error_message: str | None = None


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

    # 프로젝트 단위 폴더로 묶기: {project_id}/{session_id}/video.{ext}
    sess_row = await db.execute(select(Session).where(Session.session_id == session_id))
    sess_obj = sess_row.scalar_one_or_none()
    if not sess_obj:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    ext = ALLOWED_MIME[payload.content_type]
    s3_key = f"{sess_obj.project_id}/{session_id}/video.{ext}"
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
    if len(payload.part_numbers) > settings.UPLOAD_MAX_PARTS:
        raise HTTPException(status_code=400, detail="요청 파트 수가 너무 많아요")

    if f"/{session_id}/video." not in payload.s3_key:
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
    if f"/{session_id}/video." not in payload.s3_key:
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

    project_id = sess.project_id if sess else None
    known_actors: list[dict[str, Any]] = []
    if project_id is not None:
        known_actors = await _load_known_actors(db, project_id)

    await db.flush()
    await db.commit()

    background_tasks.add_task(
        request_face_analysis,
        video_id=video.video_id,
        session_id=session_id,
        s3_key=payload.s3_key,
        s3_url=s3_url,
        known_actors=known_actors,
        thumbnail_dir=f"{project_id}/{session_id}/" if project_id is not None else None,
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
    if f"/{session_id}/video." not in payload.s3_key:
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
    """세션의 영상 정보 + 영상에 등장한 actors 조회."""
    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")

    # 이 영상에 링크된 actors 조회 (is_new_in_video 포함)
    link_result = await db.execute(
        select(VideoActor, Actor)
        .join(Actor, Actor.actor_id == VideoActor.actor_id)
        .where(VideoActor.video_id == video.video_id)
        .order_by(Actor.actor_id)
    )
    actors_payload = [
        _build_actor_response(actor, link.is_new_in_video)
        for link, actor in link_result.all()
    ]

    return {
        "video_id": video.video_id,
        "s3_url": video.s3_url,
        "analysis_status": video.analysis_status,
        "analysis_result": _decode_analysis_result(video.analysis_result),
        "actors": actors_payload,
    }


@router.post("/analyze")
async def analyze_existing_video(
    session_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """이미 업로드된 영상을 재분석. 기존 VideoActor 링크는 삭제 후 콜백에서 재구성."""
    result = await db.execute(select(Video).where(Video.session_id == session_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")
    if not video.s3_key or not video.s3_url:
        raise HTTPException(status_code=400, detail="분석할 S3 영상 정보가 없어요")

    # 재분석 시 기존 링크 삭제 (콜백에서 재구성)
    await db.execute(delete(VideoActor).where(VideoActor.video_id == video.video_id))

    video.analysis_status = "pending"
    video.analysis_result = None

    project_id = await _project_id_for_video(db, video)
    known_actors: list[dict[str, Any]] = []
    if project_id is not None:
        known_actors = await _load_known_actors(db, project_id)

    await db.flush()
    await db.commit()

    background_tasks.add_task(
        request_face_analysis,
        video_id=video.video_id,
        session_id=session_id,
        s3_key=video.s3_key,
        s3_url=video.s3_url,
        known_actors=known_actors,
        thumbnail_dir=f"{project_id}/{session_id}/" if project_id is not None else None,
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
    """analyzer 분석 완료/실패 결과 처리.

    1. matched[] → VideoActor 링크만 추가 (썸네일/임베딩 갱신 안 함, 결정 1·2)
    2. new_candidates[] → Actor INSERT → flush로 actor_id 얻고 → name="배우 {id}"
    3. analysis_result.appearances[]의 "new:{idx}" → "actor:{id}" 치환
    4. 고아 actor (어떤 video에도 안 링크된 actor) 정리
    """
    if settings.FACE_ANALYZER_SECRET and x_analyzer_secret != settings.FACE_ANALYZER_SECRET:
        raise HTTPException(status_code=401, detail="invalid analyzer secret")

    result = await db.execute(select(Video).where(Video.video_id == payload.video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="영상을 찾을 수 없어요")

    if payload.analysis_status not in {"pending", "processing", "done", "failed"}:
        raise HTTPException(status_code=400, detail="invalid analysis_status")

    project_id = await _project_id_for_video(db, video)

    # 실패면 상태만 기록하고 종료 (링크 변경 없음)
    if payload.analysis_status != "done":
        video.analysis_status = payload.analysis_status
        if payload.error_message:
            video.analysis_result = json.dumps(
                {"error_message": payload.error_message}, ensure_ascii=False
            )
        return {"video_id": video.video_id, "analysis_status": video.analysis_status}

    if project_id is None:
        raise HTTPException(status_code=400, detail="video에 연결된 project를 찾을 수 없어요")

    # 1) matched: 기존 actor에 링크 추가 + 새 각도 exemplar 누적
    for m in payload.matched:
        actor_result = await db.execute(
            select(Actor).where(Actor.actor_id == m.actor_id, Actor.project_id == project_id)
        )
        actor = actor_result.scalar_one_or_none()
        if actor is None:
            # 다른 project의 actor_id를 보냈거나 삭제됨 → 스킵
            continue

        # 갤러리에 새 exemplar append (cap 초과 시 oldest drop).
        # 링크가 이미 있는 경우(재분석 등)에도 갤러리는 갱신.
        if m.new_exemplars:
            actor.face_embeddings = _cap_exemplars(
                list(actor.face_embeddings or []) + m.new_exemplars
            )

        existing_link = await db.execute(
            select(VideoActor).where(
                VideoActor.video_id == video.video_id,
                VideoActor.actor_id == m.actor_id,
            )
        )
        if existing_link.scalar_one_or_none() is not None:
            continue
        db.add(VideoActor(
            video_id=video.video_id,
            actor_id=m.actor_id,
            is_new_in_video=False,
        ))

    # 2) new_candidates: 새 Actor INSERT → 이름 자동 부여 → 링크
    temp_to_actor_id: dict[int, int] = {}
    for c in payload.new_candidates:
        actor = Actor(
            project_id=project_id,
            name=None,
            face_embeddings=_cap_exemplars(list(c.face_embeddings)),
            thumbnail_s3_key=c.thumbnail_s3_key,
        )
        db.add(actor)
        await db.flush()  # actor_id 확보
        actor.name = f"배우 {actor.actor_id}"
        temp_to_actor_id[c.temp_index] = actor.actor_id

        db.add(VideoActor(
            video_id=video.video_id,
            actor_id=actor.actor_id,
            is_new_in_video=True,
        ))

    # 3) analysis_result placeholder 치환
    result_payload = payload.analysis_result
    if isinstance(result_payload, dict):
        appearances = result_payload.get("appearances")
        if isinstance(appearances, list):
            for ap in appearances:
                pid = ap.get("person_id") if isinstance(ap, dict) else None
                if isinstance(pid, str) and pid.startswith("new:"):
                    try:
                        idx = int(pid.split(":", 1)[1])
                    except ValueError:
                        continue
                    if idx in temp_to_actor_id:
                        ap["person_id"] = f"actor:{temp_to_actor_id[idx]}"

    video.analysis_status = "done"
    if result_payload is not None:
        video.analysis_result = json.dumps(result_payload, ensure_ascii=False)

    await db.flush()

    # 4) 고아 actor 정리: 같은 project에서 어떤 video_actors에도 안 묶인 actor 삭제
    await db.execute(text("""
        DELETE FROM actors
        WHERE project_id = :pid
          AND actor_id NOT IN (SELECT DISTINCT actor_id FROM video_actors)
    """), {"pid": project_id})

    await db.commit()

    return {
        "video_id": video.video_id,
        "analysis_status": video.analysis_status,
        "matched_count": len(payload.matched),
        "new_actor_count": len(payload.new_candidates),
    }
