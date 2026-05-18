"""
actor.py
────────
배우(Actor) 매핑 UI 액션:
  - PATCH /actors/{id}            : 이름 수정
  - POST  /actors/{id}/merge-into : A를 B로 합치기 (사용자가 "이 둘은 같은 사람" 판정)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Actor

router = APIRouter(prefix="/actors", tags=["actors"])


class ActorRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ActorMergeRequest(BaseModel):
    target_actor_id: int


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
