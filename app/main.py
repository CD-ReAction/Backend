from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import engine, Base
from app.api.v1.endpoints import actor, camera_session, video, feedback, project, auth

app = FastAPI(title="Re:Action API", version="1.0.0")

# CORS

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-eosin-phi-egjc1ejy18.vercel.app",
        "https://reaction-camera-connection.netlify.app",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(camera_session.router, prefix=API_PREFIX)
app.include_router(video.router, prefix=API_PREFIX)
app.include_router(video.analysis_router, prefix=API_PREFIX)
app.include_router(feedback.router, prefix=API_PREFIX)
app.include_router(project.router, prefix=API_PREFIX)
app.include_router(actor.router, prefix=API_PREFIX)
app.include_router(actor.project_router, prefix=API_PREFIX)


@app.on_event("startup")
async def on_startup():
    """테이블 생성만 수행. 더미 유저는 Supabase 콘솔에서 직접 관리"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}
