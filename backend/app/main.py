"""
LumenCloud 最小骨架入口
- /api/health 健康检查
- FastAPI 静态直出前端（Vue SPA fallback）
业务 API（鉴权/审批/队列/媒体等）按 docs/新系统设计.md §9 在实施阶段注册。
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import settings
from app.database import engine, init_db
from app.scheduler import scheduler

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动护栏（fail-closed，fail-fast）：弱默认 JWT_SECRET/INIT_ADMIN_PASSWORD 拒绝启动
    from app.routers.auth import _assert_secure_secrets

    _assert_secure_secrets()

    await init_db()
    # 启动恢复：扫描 episode_state/transfer_queue，超时未完成的 transferring/downloading
    # 回退 queued + retry_count++（§4.1 / §3.1；阈值 episode_state_timeout_hours 默认 2h）
    from app.tasks.recovery import recover_on_boot
    from app.scheduler import start as start_scheduler

    await recover_on_boot()
    # 管理员初始化（幂等，§5.2）：无 admin 用户时用 INIT_ADMIN_USERNAME/PASSWORD 创建
    from app.routers.auth import ensure_admin

    await ensure_admin()
    if not scheduler.running:
        await start_scheduler()  # 包装函数：register_jobs() + scheduler.start() + _apply_job_switches()
    yield
    # wait=True：等待 APScheduler 调度线程干净退出，避免残留线程向已关闭事件循环
    # 投递；随后清空 _eventloop 引用（APScheduler 3.10 的 start() 仅在 _eventloop
    # 为 None 时刷新，shutdown 后不重置——复用同一实例会指向已关闭的旧 loop，
    # 如测试中连续 lifespan；生产单次启动不受影响）。
    scheduler.shutdown(wait=True)
    try:
        scheduler._eventloop = None  # type: ignore[attr-defined]  私有属性，修复实例复用
    except AttributeError:
        pass
    await engine.dispose()


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 路由（由另一 lane 负责：app/routers/api.py → api_router）。
# 文件尚未就绪时跳过注册，集成验证时统一接入。
try:
    from app.routers import api as api_router

    app.include_router(api_router.api_router)
except ImportError:
    # TODO(集成): routers 由 API lane 创建，集成验证时接入
    pass


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# 前端静态文件 — 必须在最后注册（SPA fallback）
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = STATIC_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")