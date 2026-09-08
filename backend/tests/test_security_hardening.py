"""安全加固单测：CORS 白名单 / 登录失败限流 / httpOnly cookie 双通道 /
健康检查 DB ping / AUTO_MIGRATE 开关 / config_store 启动重试。

依赖共享 app 单例（TestClient(app)，lifespan 自动跑 init_db → ensure_admin 等）；
各测试自备确定性 admin 密码（覆写 password_hash，不删行，避免外键引用冲突）。
限流键含 client IP + 用户名，测试用户名唯一化 + 成功登录清除，测试间互不冲突。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import config_store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """仿 test_config_store.py：每个测试前后重置 config_store 模块级缓存，
    防止 TestClient lifespan 的 load_from_db 污染跨测试状态。"""
    saved = (config_store._cache, config_store._loaded)
    config_store._cache = {}
    config_store._loaded = False
    yield
    config_store._cache, config_store._loaded = saved


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bootstrap_admin_password(password: str = "seed-pass-123") -> str:
    """确保 admin 存在并重置密码为确定性已知值（供登录断言）。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import User
    import app.routers.auth as auth_mod

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password(password)
        await session.commit()
    return password


# ---------------------------------------------------------------------------
# 1. CORS 白名单（Task 1）：白名单来源回显 ACAO，非白名单来源无 ACAO 头
# ---------------------------------------------------------------------------

def test_cors_whitelisted_origin_echoed_and_evil_origin_denied():
    with TestClient(app) as client:
        # 白名单来源（默认 vite 开发端口）→ 预检响应回显来源
        r = client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

        # 白名单第二个来源同样放行
        r = client.options(
            "/api/auth/login",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"

        # 非白名单来源 → 无 ACAO 头（浏览器拒绝跨域）
        r = client.options(
            "/api/auth/login",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# 2. 登录失败限流（Task 2）：连续失败超限 → 429；成功登录清除计数
# ---------------------------------------------------------------------------

def test_login_rate_limit_blocks_after_limit():
    with TestClient(app) as client:
        pw = client.portal.call(_bootstrap_admin_password)
        # 先成功登录一次，清掉共享 DB 可能残留的失败计数（幂等化测试起点）
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": pw}
        ).status_code == 200

        # 连续 LOGIN_FAIL_LIMIT(5) 次错误密码 → 401
        for i in range(5):
            r = client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
            )
            assert r.status_code == 401, (i, r.text)

        # 第 N+1 次（窗口内失败数 > 5）→ 429
        r = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
        )
        assert r.status_code == 429, r.text
        assert "尝试过于频繁" in r.json()["detail"]

        # 正确密码登录成功 → 200，且清除限流键（后续失败从 0 重新计数）
        r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
        assert r.status_code == 200, r.text
        r = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
        )
        assert r.status_code == 401, r.text  # 计数已重置，不再 429


# ---------------------------------------------------------------------------
# 3. httpOnly cookie 双通道（Task 3）：Set-Cookie + 无 header 走 cookie 鉴权
# ---------------------------------------------------------------------------

def test_login_sets_httponly_cookie_and_cookie_auth_works():
    with TestClient(app) as client:
        pw = client.portal.call(_bootstrap_admin_password)
        r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
        assert r.status_code == 200, r.text
        set_cookie = r.headers.get("set-cookie")
        assert set_cookie is not None
        assert "access_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        token = r.json()["access_token"]

        # 无 Authorization header，仅凭 cookie → /api/auth/me 200（双通道兜底）
        client.cookies.set("access_token", token)
        r2 = client.get("/api/auth/me")
        assert r2.status_code == 200, r2.text
        assert r2.json()["role"] == "admin"

        # header 优先：坏 header + 有效 cookie 并存 → 仍 401（以 header 为准）
        r3 = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer bad.token"},
        )
        assert r3.status_code == 401

        # 无效 cookie → 401（401 语义不变）
        client.cookies.set("access_token", "bad.token")
        r4 = client.get("/api/auth/me")
        assert r4.status_code == 401


# ---------------------------------------------------------------------------
# 4. 健康检查 DB ping（Task 4）：200 + db=true
# ---------------------------------------------------------------------------

def test_health_returns_ok_with_db_true():
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["db"] is True
        assert "time" in body


# ---------------------------------------------------------------------------
# 5. AUTO_MIGRATE 开关（Task 5）：False → init_db 不执行 Alembic 迁移
# ---------------------------------------------------------------------------

def test_init_db_skips_migration_when_auto_migrate_disabled(monkeypatch):
    import app.database as db_mod
    from alembic import command as alembic_command

    calls: list = []
    monkeypatch.setattr(alembic_command, "upgrade", lambda *a, **k: calls.append(a))

    # AUTO_MIGRATE=False → 跳过迁移（upgrade 不被调用），不抛异常
    monkeypatch.setattr(db_mod.settings, "AUTO_MIGRATE", False)
    run(db_mod.init_db())
    assert calls == []

    # AUTO_MIGRATE=True（默认/恢复）→ 照常执行迁移（upgrade 被 mock 拦截，不真跑）
    monkeypatch.setattr(db_mod.settings, "AUTO_MIGRATE", True)
    run(db_mod.init_db())
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 6. config_store 启动重试（Task 6）：首次失败 → 退避重试成功
# ---------------------------------------------------------------------------

def test_config_store_retries_then_succeeds(monkeypatch):
    attempts = {"n": 0}

    class _FlakySession:
        """首次 __aenter__ 抛异常（模拟启动期 DB 短暂不可用），之后成功。"""

        async def __aenter__(self):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("db down at startup")
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            result = MagicMock()
            result.scalars.return_value.all.return_value = [
                MagicMock(key="alist_token", value="db-token")
            ]
            return result

    monkeypatch.setattr(config_store, "async_session", _FlakySession)
    # 跳过真实退避等待（0.2s/0.4s），断言重试语义即可
    monkeypatch.setattr(config_store.asyncio, "sleep", AsyncMock())

    run(config_store.load_from_db())
    assert config_store.is_loaded()
    assert config_store.get("alist_token") == "db-token"
    assert attempts["n"] == 2  # 首次失败 + 重试成功（未等第 3 次）
