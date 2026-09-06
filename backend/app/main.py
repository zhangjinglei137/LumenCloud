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
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.database import engine, init_db
from app.scheduler import scheduler

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Phase 8：数据目录先于一切文件/JWT/DB 操作就绪。JWT 密钥在 config 模块
    # 导入时已文件化（load_or_create_jwt_secret 内部自行 mkdir，此处显式确保
    # 双保险，避免任何前置逻辑在目录缺失时操作失败）。
    Path(settings.LUMENCLOUD_DATA_DIR).mkdir(parents=True, exist_ok=True)

    # 启动护栏（fail-closed，fail-fast）：Phase 8 起 JWT 密钥自动文件化、admin
    # 初始密码随机化，不再要求 env 提供；此处仅校验文件密钥实际可用
    from app.routers.auth import _assert_secure_secrets

    _assert_secure_secrets()

    await init_db()
    # Phase 8：加载 system_config → 进程内配置缓存（services 层读取凭据的来源）。
    # 此后各 services 的 config_store.get(key, settings.X) 均读 DB 值（env 仅 fallback）；
    # settings PATCH 保存后由 settings.py 调 refresh() 增量刷新（保存即生效）。
    from app.services import config_store

    await config_store.load_from_db()
    # 启动恢复：扫描 episode_state/transfer_queue，超时未完成的 transferring/downloading
    # 回退 queued + retry_count++（§4.1 / §3.1；阈值 episode_state_timeout_hours 默认 2h）
    from app.tasks.recovery import recover_on_boot
    from app.scheduler import start as start_scheduler

    await recover_on_boot()
    # 管理员初始化（幂等，§5.2 + Phase 8）：无 admin 用户时随机生成初始密码并
    # 日志打印一次（唯一获取渠道），返回密码供调用方/测试使用
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

# API 路由：app/routers/api.py → api_router 统一注册。
# 不再捕获 ImportError（骨架期遗留）：routers 已完整实现，import/注册失败必须
# 在启动期 fail-fast 直接抛出，避免服务照常启动但 /api/* 全部 404（fail-open）。
from app.routers import api as api_router

app.include_router(api_router.api_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# 前端静态文件 — 必须在最后注册（SPA fallback）
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # P3-5：/api 未知路径返回 404 JSON（REST 语义），其余未知路径保持 SPA fallback。
        # 文件存在优先（防御性：/api 下若有静态产物仍正常直出）。
        file_path = STATIC_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        if full_path == "api" or full_path.startswith("api/"):
            # 与 FastAPI 默认错误结构一致（{"detail": "Not Found"}），
            # 前端可直接按 REST 处理，不再收到 200 的 HTML。
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return FileResponse(STATIC_DIR / "index.html")