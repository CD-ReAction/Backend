from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.database import engine, Base, AsyncSessionLocal
from app.models.models import User
from app.api.v1.endpoints import camera_session, video, feedback, project

app = FastAPI(title="Re:Action API", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
API_PREFIX = "/api/v1"
app.include_router(camera_session.router, prefix=API_PREFIX)
app.include_router(video.router, prefix=API_PREFIX)
app.include_router(feedback.router, prefix=API_PREFIX)
app.include_router(project.router, prefix=API_PREFIX)


@app.on_event("startup")
async def on_startup():
    # 1. 테이블 생성
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 더미 유저 보장 (user_id=1이 없으면 만들기)
    # TODO : main에 할때는 
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_id == 1))
        if result.scalar_one_or_none() is None:
            session.add(User(
                user_id=1,
                email="dummy@example.com",
                password="dummy",  # TODO: 인증 붙으면 해시화
            ))
            await session.commit()


@app.get("/health")
def health():
    return {"status": "ok"}