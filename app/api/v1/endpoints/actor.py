"""
actor.py
────────
배우(Actor) 매핑 UI 액션:
  - POST   /projects/{project_id}/actors : 프로젝트에 배우 수동 등록 (분석 전 사전 등록용)
  - PATCH  /actors/{id}                   : 이름 수정
  - POST   /actors/{id}/merge-into        : A를 B로 합치기 (사용자가 "이 둘은 같은 사람" 판정)
  - DELETE /actors/{id}                   : 노이즈로 잘못 잡힌 배우 제거 (매핑 화면에서 사용)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Actor, Project

router = APIRouter(prefix="/actors", tags=["actors"])
project_router = APIRouter(prefix="/projects", tags=["actors"])


class ActorCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ActorRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ActorMergeRequest(BaseModel):
    target_actor_id: int


@project_router.post("/{project_id}/actors", status_code=201)
async def create_actor(
    project_id: int,
    body: ActorCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """프로젝트에 배우 수동 등록.

    face_embeddings/thumbnail 없는 placeholder로 생성. 영상 분석 매칭에는 사용되지 않고,
    프론트에서 actor_id를 사전에 알고 쓰기 위한 용도.
    """
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없어요")

    actor = Actor(
        project_id=project_id,
        name=body.name.strip(),
    )
    db.add(actor)
    await db.flush()
    await db.commit()

    return {
        "actor_id": actor.actor_id,
        "project_id": actor.project_id,
        "name": actor.name,
    }


@project_router.get("/{project_id}/actors")
async def list_project_actors(
    project_id: int,
    db: AsyncSession = Depends(get_db),
):
    """프로젝트에 속한 배우 목록 조회 (피드백 작성 시 태그 후보로 사용)"""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없어요")

    result = await db.execute(
        select(Actor)
        .where(Actor.project_id == project_id)
        .order_by(Actor.actor_id)
    )
    actors = result.scalars().all()

    return [
        {
            "actor_id": a.actor_id,
            "project_id": a.project_id,
            "name": a.name,
            "thumbnail_s3_key": a.thumbnail_s3_key,
        }
        for a in actors
    ]


@router.patch("/{actor_id}")
async def rename_actor(
    actor_id: int,
    body: ActorRenameRequest,
    db: AsyncSession = Depends(get_db),
):
    """배우 이름 수정 ('배우 5' → '이예나')"""
    result = await db.execute(select(Actor).where(Actor.actor_id == actor_id))
    actor = result.scalar_one_or_none()
    if not actor:
        raise HTTPException(status_code=404, detail="배우를 찾을 수 없어요")

    actor.name = body.name.strip()
    await db.flush()
    await db.commit()

    return {
        "actor_id": actor.actor_id,
        "name": actor.name,
    }


@router.post("/{actor_id}/merge-into")
async def merge_actor(
    actor_id: int,
    body: ActorMergeRequest,
    db: AsyncSession = Depends(get_db),
):
    """Actor A(actor_id)를 B(target_actor_id)로 합침.

    처리 순서 (UNIQUE(video_id, actor_id) 충돌 회피):
      1. A의 VideoActor 중 B와 충돌 안 하는 것만 actor_id를 B로 UPDATE
         (B가 이미 그 video에 링크돼 있으면 UPDATE 안 함 → A 링크는 그대로 남음)
         → is_new_in_video는 B쪽 값 그대로 유지됨 (B 행을 안 건드림)
      2. A의 남은(=충돌해서 못 옮긴) VideoActor 행 전부 DELETE
      3. Actor A DELETE
    """
    if actor_id == body.target_actor_id:
        raise HTTPException(status_code=400, detail="자기 자신으로는 merge할 수 없어요")

    # 두 actor 존재 + 같은 project 확인
    result = await db.execute(
        select(Actor).where(Actor.actor_id.in_([actor_id, body.target_actor_id]))
    )
    actors = {a.actor_id: a for a in result.scalars().all()}
    src = actors.get(actor_id)
    dst = actors.get(body.target_actor_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="배우를 찾을 수 없어요")
    if src.project_id != dst.project_id:
        raise HTTPException(status_code=400, detail="같은 프로젝트의 배우끼리만 merge 가능해요")

    # 1) 충돌 안 하는 링크만 B로 이전
    await db.execute(text("""
        UPDATE video_actors
        SET actor_id = :dst_id
        WHERE actor_id = :src_id
          AND video_id NOT IN (
              SELECT video_id FROM video_actors WHERE actor_id = :dst_id
          )
    """), {"src_id": actor_id, "dst_id": body.target_actor_id})

    # 2) 남은 A 링크 정리 (B와 같은 video에 둘 다 있었던 경우)
    await db.execute(text("""
        DELETE FROM video_actors WHERE actor_id = :src_id
    """), {"src_id": actor_id})

    # 3) A 본인 삭제
    await db.execute(text("""
        DELETE FROM actors WHERE actor_id = :src_id
    """), {"src_id": actor_id})

    await db.commit()

    return {
        "merged_from": actor_id,
        "merged_into": body.target_actor_id,
    }


@router.delete("/{actor_id}")
async def delete_actor(
    actor_id: int,
    db: AsyncSession = Depends(get_db),
):
    """배우 삭제 (analyzer가 노이즈를 얼굴로 잡아 가짜 배우가 생긴 경우 매핑 화면에서 제거).

    cascade로 VideoActor / FeedbackActor 링크도 함께 삭제됨.
    다음 영상 분석에서 같은 얼굴이 다시 잡히면 새 Actor로 등록됨 (= 갤러리 초기화).
    """
    actor = await db.get(Actor, actor_id)
    if not actor:
        raise HTTPException(status_code=404, detail="배우를 찾을 수 없어요")

    await db.delete(actor)
    await db.commit()

    return {"deleted_actor_id": actor_id}
