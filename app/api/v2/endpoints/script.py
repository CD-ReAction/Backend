"""
script.py (v2)
──────────────
프로젝트 대본(PDF) 업로드/조회.

- 프로젝트당 1개, 업로드 후 수정/교체 불가 (MVP)
- 파일이 작으므로(수 MB) 영상과 달리 presigned multipart 없이 서버 경유로 S3 업로드
- S3 키: {project_id}/script.pdf (영상 {project_id}/{session_id}/video.* 컨벤션의 한 단계 위)
- GET 404 = 대본 없음 → 프론트가 기존 UI / 대본 UI 분기하는 신호
"""

from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.realtime import manager
from app.core.s3 import generate_presigned_url, upload_object
from app.models.models import Project, Script

router = APIRouter(prefix="/projects/{project_id}/script", tags=["script"])

MAX_SCRIPT_SIZE_MB = 20


def _utc_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return f"{dt.isoformat()}Z"


def _script_response(s: Script, with_url: bool = True) -> dict:
    out = {
        "script_id": s.script_id,
        "project_id": s.project_id,
        "filename": s.filename,
        "page_count": s.page_count,
        "uploaded_by_user_id": s.uploaded_by_user_id,
        "created_at": _utc_iso(s.created_at),
    }
    if with_url:
        out["url"] = generate_presigned_url(s.s3_key)
    return out


@router.post("", status_code=201)
async def upload_script(
    project_id: int,
    file: UploadFile = File(...),
    page_count: int | None = Query(default=None, description="FE가 파악한 PDF 페이지 수 (선택)"),
    user_id: int | None = Query(default=None, description="업로더 user_id (선택)"),
    db: AsyncSession = Depends(get_db),
):
    """프로젝트에 대본 PDF 업로드. 이미 있으면 409 (MVP: 교체 불가)."""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없어요")

    result = await db.execute(select(Script).where(Script.project_id == project_id))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="이미 대본이 업로드된 프로젝트예요")

    body = await file.read()
    if not body:
        raise HTTPException(status_code=400, detail="빈 파일이에요")
    if len(body) > MAX_SCRIPT_SIZE_MB * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail=f"대본은 {MAX_SCRIPT_SIZE_MB}MB 이하만 업로드할 수 있어요",
        )
    # content-type은 브라우저마다 제각각이라 매직 바이트로 판별
    if not body.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="PDF 파일만 업로드할 수 있어요")

    s3_key = f"{project_id}/script.pdf"
    await upload_object(s3_key, body, "application/pdf")

    script = Script(
        project_id=project_id,
        s3_key=s3_key,
        filename=file.filename,
        page_count=page_count,
        uploaded_by_user_id=user_id,
    )
    db.add(script)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError:
        # 동시 업로드 race → unique(project_id) 충돌
        await db.rollback()
        raise HTTPException(status_code=409, detail="이미 대본이 업로드된 프로젝트예요")

    # 같은 프로젝트에 들어와 있는 멤버 화면 실시간 전환용
    await manager.broadcast(
        "script.uploaded",
        project_id,
        None,
        _script_response(script, with_url=False),
    )

    return _script_response(script)


@router.get("")
async def get_script(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """대본 메타 + presigned URL 조회. 없으면 404 (→ 프론트 UI 분기 신호)."""
    result = await db.execute(select(Script).where(Script.project_id == project_id))
    script = result.scalar_one_or_none()
    if not script:
        raise HTTPException(status_code=404, detail="이 프로젝트에는 대본이 없어요")
    return _script_response(script)
