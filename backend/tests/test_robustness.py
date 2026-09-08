"""生产健壮性回归（三缺口修复）：

1. capacity._pending_estimate_gb 无异常防护：transfer_queue 查询失败 → 此前
   /api/capacity 直接 500（前端 30s 轮询持续刷错）。现与 _reserved_gb 一致：
   失败 logger.warning + 返回 None（pending 预估为增强字段，失败不 500）。
2. deps.get_session 无异常防护：DB 连接失败裸抛 500 → 现 503「数据库不可用」。
3. main.py 无日志配置：docker logs 看不到 access log / traceback → 现显式把
   app logger 与 uvicorn logger 接向 stderr。

数据库隔离与 test_api_smoke 一致：模块导入前把 LUMENCLOUD_DATA_DIR 指向独立
临时目录（app.database 模块级 engine 创建时读取），不碰生产数据；TestClient
lifespan 由 with 上下文自动触发（init_db → recover → ensure_admin → scheduler）。
"""
import asyncio
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_robustness_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["JWT_SECRET"] = "robustness-secret-change-me-12345"
os.environ["INIT_ADMIN_USERNAME"] = "admin"
# 隔离外部服务（空凭据 → 容量 provider 走 unavailable，capacity 端点仍 200）
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_USERNAME"] = ""
os.environ["CLOUDSAVER_PASSWORD"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""
os.environ["ALIST_BASE_URL"] = ""
os.environ["ALIST_TOKEN"] = ""
os.environ["ARIA2_RPC_URL"] = ""
os.environ["ARIA2_TOKEN"] = ""
os.environ["NASTOOLS_BASE_URL"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""

import pytest  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402  也触发 main.py 模块级日志配置（basicConfig）
from app.routers.capacity import _pending_estimate_gb  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。"""
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import User
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None  # 已删除 admin，必然重新创建并返回初始密码
    return password


def _login_admin(client: TestClient) -> str:
    password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# 1) capacity：pending 预估失败不 500
# ---------------------------------------------------------------------------

def test_pending_estimate_gb_internal_error_returns_none():
    """函数级：内部 DB 查询抛错 → 返回 None 而非上抛（增强字段 fail-open）。"""
    async def scenario():
        session = MagicMock()
        session.execute = AsyncMock(side_effect=RuntimeError("transfer_queue 表缺失"))
        assert await _pending_estimate_gb(session) is None

    asyncio.run(scenario())


def test_capacity_pending_failure_keeps_200(monkeypatch):
    """端点级：pending 查询抛错（列不匹配场景）→ /api/capacity 仍 200。

    真实模拟「transfer_queue 表结构不匹配」：monkeypatch _load_transfer_queue_model
    返回列属性访问即抛错的伪模型（getattr 发生在函数内部 try 块内），而非替换整个
    _pending_estimate_gb（那样异常在端点层裸抛才是真 500）——验证的是产品函数自身的
    防护已生效。正常路径（首个请求）不受影响。
    """
    import app.routers.capacity as cap_mod

    with TestClient(app) as client:
        headers = _auth(_login_admin(client))

        # 正常路径：字段存在且为数值；端点整体 200
        r = client.get("/api/capacity", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["source"] in ("alist", "unavailable", "estimated")
        assert "pending_estimate" in body
        assert body["pending_estimate"] is None or isinstance(body["pending_estimate"], float)

        # 异常路径：内部查询抛错 → pending_estimate=None，不 500
        class _ExplodingMeta(type):
            def __getattr__(cls, name):
                raise RuntimeError(f"transfer_queue {name} 列不匹配")

        class _BrokenTransferQueue(metaclass=_ExplodingMeta):
            pass

        monkeypatch.setattr(cap_mod, "_load_transfer_queue_model", lambda: _BrokenTransferQueue)
        r = client.get("/api/capacity", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pending_estimate"] is None
        assert body["source"] in ("alist", "unavailable", "estimated")
        assert "reserved_gb" in body and "recent_snapshots" in body  # 其它增强字段未受影响


def test_capacity_decimal_reserved_gb_no_500(monkeypatch):
    """PG 回归（容量 500 真根因）：alist 就绪（total/used 为 float）时，若
    reserved 返回 decimal.Decimal（PG 下 func.sum(file_size) 为 NUMERIC →
    asyncpg 返回 Decimal；SQLite 返回 int 故本地测试不显），端点混合算术
    float - Decimal 曾抛 TypeError → 生产 /api/capacity 500（前端 30s 轮询
    持续刷错）。修复后统一转 float，available_gb 正常计算。
    """
    from decimal import Decimal

    import app.routers.capacity as cap_mod

    async def _usage_ok():
        return {
            "source": "alist",
            "total_gb": 210.0,
            "used_gb": 1.28,
            "checked_at": "2026-09-08T05:00:00.000000",
        }

    async def _reserved_decimal(_session):
        return Decimal("0.5")

    monkeypatch.setattr(cap_mod, "_get_usage", _usage_ok)
    monkeypatch.setattr(cap_mod, "_reserved_gb", _reserved_decimal)

    with TestClient(app) as client:
        headers = _auth(_login_admin(client))
        r = client.get("/api/capacity", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body["available_gb"], float)
        assert body["reserved_gb"] == 0.5
        assert body["available_gb"] == round(210.0 - 1.28 - 0.5, 2)


# ---------------------------------------------------------------------------
# 2) deps.get_session：DB 不可用 → 503（而非裸 500）
# ---------------------------------------------------------------------------

def test_get_session_db_failure_returns_503(monkeypatch):
    """monkeypatch async_session 构造抛异常（连接失败）→ 依赖 get_session 的
    /api/auth/me 返回 503「数据库不可用」，而非 500。"""
    with TestClient(app) as client:  # lifespan 先以真实 async_session 完成初始化
        def _boom():
            raise RuntimeError("数据库连接失败")

        monkeypatch.setattr("app.routers.deps.async_session", _boom)
        r = client.get("/api/auth/me")
        assert r.status_code == 503, r.text
        assert r.json().get("detail") == "数据库不可用"


# ---------------------------------------------------------------------------
# 3) 日志：main.py 配置已生效（root → stderr，app/uvicorn logger 可上溯）
# ---------------------------------------------------------------------------

def test_app_logging_configured_to_stderr():
    import logging

    root = logging.getLogger()
    assert root.handlers, "main.py 日志配置应使 root logger 至少挂一个 handler"
    assert any(
        getattr(h, "stream", None) is sys.stderr for h in root.handlers
    ), "root logger 的 handler 应指向 stderr（docker 捕获）"

    import app.routers.capacity as cap_mod

    # app 模块 logger：默认 propagate 到 root 的 stderr handler（业务日志/traceback 可出）
    assert cap_mod.logger.propagate is True

    # uvicorn loggers：propagate 兜底打开（默认 LOGGING_CONFIG 已自带 stderr/stdout
    # handler 时无双写；自定义 --log-config 移除 handler 时不丢失）
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate is True, name