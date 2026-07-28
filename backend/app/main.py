from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.config import settings
from app.database import engine, Base
from app.api.setup import router as setup_router
from app.api.auth import router as auth_router
from app.api.media import router as media_router
from app.api.subscription import router as subscription_router
from app.api.interaction import router as interaction_router
from app.api.admin import router as admin_router
from app.api.task import router as task_router
from app.api.notification import router as notification_router
from app.api.webhook import router as webhook_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(setup_router)  # 无需认证，必须在最前面注册
app.include_router(auth_router)
app.include_router(media_router)
app.include_router(subscription_router)
app.include_router(interaction_router)
app.include_router(admin_router)
app.include_router(task_router)
app.include_router(notification_router)
app.include_router(webhook_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}

# 静默处理 CodeGraph CPG 分析请求（非 LumenCloud 业务）
@app.api_route("/api/v1/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def codegraph_noop(rest: str):
    return {}

# 前端静态文件 — 必须在最后注册（SPA fallback）
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = STATIC_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")
