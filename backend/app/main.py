"""
LumenCloud 最小骨架入口
- /api/health 健康检查
- FastAPI 静态直出前端（Vue SPA fallback）
业务 API（鉴权/审批/队列/媒体等）按 docs/新系统设计.md §9 在实施阶段注册。
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import async_session, engine, init_db
from app.scheduler import scheduler

# ---- 生产可观测性（docker logs 必须能见 access log + traceback）----
# 此前 main.py 依赖模块顶层 logging.basicConfig + uvicorn 的 dictConfig「恰好
# 不含 root 键、不覆盖 root handlers」这一内部行为存活。D4（审查 E6）重构为
# 显式 handler 配置 _configure_logging()：模块顶层与 lifespan 开头各调用一次，
# 幂等——即便部署以 --log-config 移除了 root handler 或改写 uvicorn logger，
# 启动后 root 仍有 stderr handler、uvicorn logger 的 propagate 兜底保持打开，
# 不再依赖 uvicorn 内部配置行为；日志输出与级别与原 basicConfig 行为等价。
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _configure_logging() -> None:
    """显式挂载 root logger → stderr（幂等，可重复调用）。

    handler 带 _lumencloud 标记：幂等判断（有标记则不重复挂）+ 测试可断言
    「该 handler 是 LumenCloud 显式挂载（非 uvicorn/第三方默认）」。stderr 是
    docker 错误信息最稳落点（默认同时捕获 stdout+stderr）。
    """
    root = logging.getLogger()
    if not any(getattr(h, "_lumencloud", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        handler._lumencloud = True  # type: ignore[attr-defined]  显式挂载标记
        root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # uvicorn logger propagate 兜底：若部署以 --log-config 移除了自带的
    # stderr/stdout handler，日志仍会上溯到 root 的 stderr handler，不丢失
    for _name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(_name).propagate = True


_configure_logging()
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # D4（审查 E6）：lifespan 内再次显式配置日志（幂等）——不依赖 uvicorn
    # dictConfig 行为；即使 uvicorn/第三方以 --log-config 改写过 root handlers，
    # 启动完成后 root 仍有 stderr handler / uvicorn propagate 兜底。
    _configure_logging()
    # Phase 8：数据目录先于一切文件/JWT/DB 操作就绪。JWT 密钥在 config 模块
    # 导入时已文件化（load_or_create_jwt_secret 内部自行 mkdir，此处显式确保
    # 双保险，避免任何前置逻辑在目录缺失时操作失败）。
    Path(settings.LUMENCLOUD_DATA_DIR).mkdir(parents=True, exist_ok=True)

    # 启动护栏（fail-closed，fail-fast）：Phase 8 起 JWT 密钥自动文件化、admin
    # 初始密码随机化，不再要求 env 提供；此处仅校验文件密钥实际可用
    from app.routers.auth import _assert_secure_secrets

    _assert_secure_secrets()

    await init_db()
    # D4（审查 E6）：alembic 迁移的 env.py 会 fileConfig 重置 root logger
    # handlers（alembic.ini 未配置 root），init_db 后再次显式恢复挂载（幂等），
    # 保证后续业务日志始终落到 stderr handler（幂等不重复挂）。
    _configure_logging()
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


# docker-timezone：统一 UTC+Z 时间序列化（naive 补 Z / aware 归一 Z）
from app.json import install_zulu_encoder

install_zulu_encoder()

app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# 安全加固（Task 1）：CORS 白名单从 settings.CORS_ALLOW_ORIGINS 读取（逗号分隔）。
# 白名单 + allow_credentials=True 是允许的组合（"*" + credentials 才是浏览器禁止
# 的组合）。env 置空串 → 白名单为空 → 不注册 CORS 中间件：生产同源部署由 FastAPI
# 直出静态页，浏览器不发 Origin，无需 CORS，完全禁用更安全。
_cors_origins = [o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
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
    """健康检查：探活数据库（SELECT 1，3 秒超时），保持轻量。

    - DB 正常 → 200 {"status": "ok", "db": true, "time": ...}
    - DB 异常 → 503 {"status": "degraded", "db": false}
    不探测 aria2/alist 等外部服务（避免健康检查因下游抖动误报）。
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        async with async_session() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3)
        return {"status": "ok", "db": True, "time": now}
    except Exception as exc:  # noqa: BLE001  健康检查失败即 degraded，不阻断请求
        logger.warning("健康检查 DB 探测失败: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "db": False, "time": now},
        )


# 前端静态文件 — 必须在最后注册（SPA fallback）
# /assets 挂载需要目录真实存在（StaticFiles 初始化会校验目录），保持存在性门控。
# serve_spa 路由则无条件注册：backend/static 是前端构建产物（gitignore 不入库），
# CI 干净 checkout 下目录不存在——若把 fallback 路由也放进存在性门控，路由整体不
# 注册，所有非 API 路径一律 404，且「路径穿越防护」形同虚设（请求根本到不了
# serve_spa）。无条件注册后：目录缺失时最多回退 404 JSON，绝不 500。
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    # P3-5：/api 未知路径返回 404 JSON（REST 语义），其余未知路径保持 SPA fallback。
    # D7 安全审查（路径穿越防护）：URL 编码的 `..%2f` 未经规范化直接 join 可
    # 穿越出 static 根读取任意文件（如 <data_dir>/.jwt_secret 伪造 token）。
    # 先 resolve 规范化 + 必须落在 static 根内，越界一律回退 SPA（不直出文件）。
    static_root = STATIC_DIR.resolve()
    file_path = (static_root / full_path).resolve()
    if file_path.is_relative_to(static_root) and file_path.is_file():
        return FileResponse(file_path)
    if full_path == "api" or full_path.startswith("api/"):
        # 与 FastAPI 默认错误结构一致（{"detail": "Not Found"}），
        # 前端可直接按 REST 处理，不再收到 200 的 HTML。
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    if full_path == "internal" or full_path.startswith("internal/"):
        # D4（审查 E10）：/internal 前缀为内部回调区（aria2/nastools webhook 等
        # 显式注册的 POST 路由，经 api 聚合 router 挂载，不经过本 catch-all）。
        # 未注册的 /internal GET 路径同样返回 404 JSON（与 /api 前缀一致），
        # 避免被 SPA fallback 吞成 200 HTML（前端误判为页面存在）。
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    # static 根缺失（CI 无产物/未构建前端）：无 index.html 可回退，
    # 返回与 FastAPI 默认一致的 404 JSON，避免 FileResponse 缺文件抛 500。
    return JSONResponse(status_code=404, content={"detail": "Not Found"})