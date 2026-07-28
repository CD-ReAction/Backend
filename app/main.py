from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import engine, Base
from app.api import ws
from app.api.v1.endpoints import actor, camera_session, video, feedback, project, auth
from app.api.v2.endpoints import feedback as feedback_v2, script as script_v2

app = FastAPI(title="Re:Action API", version="1.0.0")

# CORS

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cau-cd-reaction.vercel.app",
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
app.include_router(actor.session_router, prefix=API_PREFIX)

# v2: 대본(PDF) 기반 플로우 (대본 없는 프로젝트는 기존 v1 사용 — UI 분기)
API_V2_PREFIX = "/api/v2"
app.include_router(script_v2.router, prefix=API_V2_PREFIX)
app.include_router(feedback_v2.router, prefix=API_V2_PREFIX)
app.include_router(feedback_v2.project_router, prefix=API_V2_PREFIX)

# WebSocket (프론트 VITE_WS_URL → ws(s)://<host>/ws, API_PREFIX 없이 루트에 마운트)
app.include_router(ws.router)


@app.on_event("startup")
async def on_startup():
    """테이블 생성만 수행. 더미 유저는 Supabase 콘솔에서 직접 관리"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}
