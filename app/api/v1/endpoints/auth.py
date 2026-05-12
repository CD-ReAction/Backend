"""
auth.py
───────
로그인 (데모용: 평문 비밀번호 비교)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.database import get_db
from app.models.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


# ── 스키마 ──────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    user_id: int
    email: str


# ── 엔드포인트 ───────────────────────────────────────────

@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """이메일/비밀번호로 로그인 (데모용)"""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or user.password != body.password:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않아요")

    return LoginResponse(user_id=user.user_id, email=user.email)