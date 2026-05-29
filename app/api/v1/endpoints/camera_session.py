"""
camera_session.py
─────────────────
QR 코드 기반 핸드폰 카메라 연결용 세션 관리
DB 저장 (Supabase camera_sessions 테이블)

상태 흐름:
  yet → connect → recording → end → done | expired
"""

import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import CameraSession

router = APIRouter(prefix="/camera-session", tags=["camera-session"])


# ── 스키마 ──────────────────────────────────────────────

class CameraSessionResponse(BaseModel):
    session_id: str
    code: str
    camera_url: str
    expires_at: str
    db_session_id: int


class CameraStatusResponse(BaseModel):
    session_id: str
    status: str  # yet | connect | recording | end | done | expired
    connected_at: Optional[str] = None
    recording_started_at: Optional[str] = None
    recording_elapsed_seconds: Optional[int] = None
    video_url: Optional[str] = None


# ── 헬퍼 ────────────────────────────────────────────────

def _make_code() -> str:
    chars = (
        string.ascii_uppercase.replace("O", "").replace("I", "")
        + string.digits.replace("0", "")
    )
    return "".join(secrets.choice(chars) for _ in range(4))


async def _get_session(session_id: str, db: AsyncSession) -> CameraSession:
    result = await db.execute(
        select(CameraSession).where(CameraSession.id == session_id)
    )
    s = result.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")
    return s


async def _create_new_session(
    db_session_id: int,
    pwa_base_url: str,
    db: AsyncSession,
) -> CameraSession:
    """새 camera_session 생성"""
    session_id = secrets.token_urlsafe(12)
    code = _make_code()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    camera_url = (
        f"{pwa_base_url}/camera.html"
        f"?session={session_id}&code={code}&db_session={db_session_id}"
    )
    s = CameraSession(
        id=session_id,
        db_session_id=db_session_id,
        code=code,
        camera_url=camera_url,
        status="yet",
        expires_at=expires_at,
    )
    db.add(s)
    await db.flush()
    await db.commit()
    return s


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("/create", response_model=CameraSessionResponse)
async def create_camera_session(
    db_session_id: int,
    pwa_base_url: str = "https://reaction-camera-connection.netlify.app",
    db: AsyncSession = Depends(get_db),
):
    """노트북에서 호출 — QR용 세션 생성 (status: yet)"""
    s = await _create_new_session(db_session_id, pwa_base_url, db)
    return CameraSessionResponse(
        session_id=s.id,
        code=s.code,
        camera_url=s.camera_url,
        expires_at=s.expires_at.isoformat(),
        db_session_id=s.db_session_id,
    )


@router.get("/by-db-session/{db_session_id}", response_model=CameraSessionResponse)
async def get_or_create_camera_session(
    db_session_id: int,
    pwa_base_url: str = "https://reaction-camera-connection.netlify.app",
    db: AsyncSession = Depends(get_db),
):
    """
    db_session_id로 활성 camera_session 조회.
    없거나 만료됐으면 새로 생성.
    
    다인원 접속 동기화 핵심 엔드포인트:
    - 여러 명이 같은 workspace에 입장해도 동일한 camera_session 반환
    - 프론트에서 /create 대신 이 엔드포인트를 사용하면 됨
    """
    result = await db.execute(
        select(CameraSession)
        .where(
            CameraSession.db_session_id == db_session_id,
            CameraSession.status.notin_(["expired", "done"]),
        )
        .order_by(CameraSession.expires_at.desc())
    )
    existing = result.scalar_one_or_none()

    # 만료 체크
    if existing and datetime.utcnow() > existing.expires_at:
        existing.status = "expired"
        await db.commit()
        existing = None

    # 없으면 새로 생성
    if not existing:
        existing = await _create_new_session(db_session_id, pwa_base_url, db)

    return CameraSessionResponse(
        session_id=existing.id,
        code=existing.code,
        camera_url=existing.camera_url,
        expires_at=existing.expires_at.isoformat(),
        db_session_id=existing.db_session_id,
    )


@router.get("/{session_id}/status", response_model=CameraStatusResponse)
async def get_camera_session_status(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """노트북이 폴링 — 현재 상태 반환"""
    s = await _get_session(session_id, db)
    now = datetime.utcnow()

    if s.status != "expired" and now > s.expires_at:
        s.status = "expired"
        await db.commit()

    recording_elapsed_seconds = None
    if s.status == "recording" and s.recording_started_at:
        recording_elapsed_seconds = max(
            0,
            int((now - s.recording_started_at).total_seconds()),
        )

    return CameraStatusResponse(
        session_id=s.id,
        status=s.status,
        connected_at=s.connected_at.isoformat() if s.connected_at else None,
        recording_started_at=s.recording_started_at.isoformat() if s.recording_started_at else None,
        recording_elapsed_seconds=recording_elapsed_seconds,
        video_url=s.video_url,
    )


@router.post("/{session_id}/connect")
async def mark_connected(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """핸드폰 QR 접속 직후 자동 호출 → status: connect"""
    s = await _get_session(session_id, db)
    s.status = "connect"
    s.connected_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "status": "connect"}


@router.post("/{session_id}/recording")
async def mark_recording(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """촬영 버튼 누를 때 호출 → status: recording"""
    s = await _get_session(session_id, db)
    s.status = "recording"
    if not s.recording_started_at:
        s.recording_started_at = datetime.utcnow()
    await db.commit()
    return {
        "ok": True,
        "status": "recording",
        "recording_started_at": s.recording_started_at.isoformat(),
    }


@router.post("/{session_id}/stop")
async def mark_stop(
    session_id: str,
    db: AsyncSession = Depends(get_db),
):
    """녹화 종료 버튼 누를 때 호출 → status: end"""
    s = await _get_session(session_id, db)
    s.status = "end"
    await db.commit()
    return {"ok": True, "status": "end"}


@router.post("/{session_id}/done")
async def mark_done(
    session_id: str,
    video_url: str = "",
    db: AsyncSession = Depends(get_db),
):
    """영상 업로드 완료 후 호출 → status: done"""
    s = await _get_session(session_id, db)
    s.status = "done"
    s.video_url = video_url
    await db.commit()
    return {"ok": True, "status": "done"}
