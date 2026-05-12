"""
camera_session.py
─────────────────
QR 코드 기반 핸드폰 카메라 연결용 임시 세션 관리
인메모리 저장 (Redis 도입 전까지 사용)
"""

import secrets
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/camera-session", tags=["camera-session"])

# 인메모리 저장소
_sessions: dict = {}


# ── 스키마 ──────────────────────────────────────────────

class CameraSessionResponse(BaseModel):
    session_id: str
    code: str
    camera_url: str
    expires_at: str
    db_session_id: int  # DB 세션 ID


class CameraStatusResponse(BaseModel):
    session_id: str
    status: str  # waiting | connected | recording | done | expired
    connected_at: Optional[str] = None
    video_url: Optional[str] = None


# ── 헬퍼 ────────────────────────────────────────────────

def _make_code() -> str:
    chars = (
        string.ascii_uppercase.replace("O", "").replace("I", "")
        + string.digits.replace("0", "")
    )
    return "".join(secrets.choice(chars) for _ in range(4))


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("/create", response_model=CameraSessionResponse)
def create_camera_session(
    db_session_id: int,  # DB sessions 테이블의 session_id
    pwa_base_url: str = "https://reaction-camera-connection.netlify.app",
):
    """노트북에서 호출 — QR용 임시 세션 생성"""
    session_id = secrets.token_urlsafe(12)
    code = _make_code()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    camera_url = (
        f"{pwa_base_url}/camera.html"
        f"?session={session_id}&code={code}&db_session={db_session_id}"
    )

    _sessions[session_id] = {
        "session_id": session_id,
        "db_session_id": db_session_id,
        "code": code,
        "camera_url": camera_url,
        "status": "waiting",
        "expires_at": expires_at,
        "connected_at": None,
        "video_url": None,
    }

    return CameraSessionResponse(
        session_id=session_id,
        code=code,
        camera_url=camera_url,
        expires_at=expires_at.isoformat(),
        db_session_id=db_session_id,
    )


@router.get("/{session_id}/status", response_model=CameraStatusResponse)
def get_camera_session_status(session_id: str):
    """노트북이 1초마다 폴링 — 핸드폰 연결/완료 여부 확인"""
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    if datetime.utcnow() > s["expires_at"]:
        s["status"] = "expired"

    return CameraStatusResponse(
        session_id=session_id,
        status=s["status"],
        connected_at=s["connected_at"],
        video_url=s["video_url"],
    )


@router.post("/{session_id}/connect")
def mark_connected(session_id: str):
    """핸드폰이 QR 접속 직후 자동 호출 → 노트북 화면 '연결됨' 전환"""
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    s["status"] = "connected"
    s["connected_at"] = datetime.utcnow().isoformat()
    return {"ok": True}


@router.post("/{session_id}/done")
def mark_done(session_id: str, video_url: str = ""):
    """핸드폰이 영상 업로드 완료 후 호출 → 노트북 화면 매핑 전환"""
    s = _sessions.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없어요")

    s["status"] = "done"
    s["video_url"] = video_url
    return {"ok": True}