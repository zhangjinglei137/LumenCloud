"""Phase 8 认证增强单测：JWT 密钥文件化 / admin 随机初始密码 / 修改密码 API。

覆盖：
- load_or_create_jwt_secret：首次生成（64 位 hex、chmod 600）、重复读取幂等、
  读回内容一致；传入子目录自动 mkdir。
- ensure_admin：随机 16 位初始密码（bcrypt 入库可验）、幂等（已存在返回 None）、
  已存在用户密码不被覆盖。
- POST /api/auth/change-password：未登录 401 / 旧密码错误 401 / 成功 / 新密码
  过短 422 / 改密后旧密码失效新密码可登录。
"""
import asyncio
import logging
import stat
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import load_or_create_jwt_secret
from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.routers.auth as auth_mod
from app.models import User


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# JWT 密钥文件化（交付 1，纯函数，不依赖 DB）
# ---------------------------------------------------------------------------

def test_jwt_secret_generate_reload_and_mode(tmp_path):
    """首次生成：64 位 hex、chmod 600；重启读取幂等（永久有效）。"""
    s1 = load_or_create_jwt_secret(str(tmp_path))
    assert len(s1) >= 64  # token_hex(32)
    assert set(s1) <= set("0123456789abcdef")

    path = tmp_path / ".jwt_secret"
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == s1
    # chmod 600：owner 可读写，group/other 无任何权限
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    # 二次调用（模拟重启）读到同一值，不重新生成
    s2 = load_or_create_jwt_secret(str(tmp_path))
    assert s2 == s1


def test_jwt_secret_mkdir_parents(tmp_path):
    """数据目录不存在时自动创建（config import 早于 main.py lifespan 的兜底）。"""
    nested = tmp_path / "a" / "b"
    secret = load_or_create_jwt_secret(str(nested))
    assert len(secret) >= 64
    assert (nested / ".jwt_secret").exists()


# ---------------------------------------------------------------------------
# ensure_admin 随机初始密码（交付 2）
# ---------------------------------------------------------------------------

@pytest.fixture()
def auth_db(monkeypatch):
    """独立 in-memory SQLite，monkeypatch auth 模块内的 async_session。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    run(_create())
    monkeypatch.setattr(auth_mod, "async_session", maker)
    yield maker
    run(engine.dispose())


async def _admin_user(session):
    return await session.scalar(select(User).where(User.role == "admin"))


def test_ensure_admin_random_password_and_idempotent(auth_db):
    """首次创建：随机密码可验证、bcrypt 入库；再次调用幂等返回 None。"""
    password = run(auth_mod.ensure_admin())
    assert password is not None
    assert len(password) == 16
    # 字符集内（避免易混淆字符）
    assert set(password) <= set(auth_mod._ADMIN_PASSWORD_CHARS)

    async def _check():
        async with auth_db() as session:
            user = await _admin_user(session)
            assert user is not None and user.username == "admin"
            assert auth_mod.verify_password(password, user.password_hash)
            return user

    run(_check())

    # 幂等：已存在 admin → 返回 None，密码不被覆盖
    assert run(auth_mod.ensure_admin()) is None
    async def _verify_unchanged():
        async with auth_db() as session:
            user = await _admin_user(session)
            return auth_mod.verify_password(password, user.password_hash)
    assert run(_verify_unchanged()) is True


def test_ensure_admin_keeps_existing_password(auth_db):
    """已有 admin（非随机密码场景）时不改动其密码。"""
    async def _seed():
        async with auth_db() as session:
            session.add(User(username="root", password_hash=auth_mod.hash_password("fixed-pass-1"), role="admin"))
            await session.commit()
    run(_seed())

    assert run(auth_mod.ensure_admin()) is None
    async def _check():
        async with auth_db() as session:
            user = await _admin_user(session)
            assert user.username == "root"
            assert auth_mod.verify_password("fixed-pass-1", user.password_hash)
    run(_check())


def test_ensure_admin_existing_logs_skip(auth_db, monkeypatch):
    """已有 admin → 跳过分支打印明确日志（「跳过」而非静默/失败，Q9 线上排障）。

    用户「清空数据库」重启未看到新初始密码：若 users 表残留 admin 行则命中本分支，
    日志应出现「已存在管理员用户，跳过初始化」，便于区分「未触发」与「初始化失败」。

    ⚠️ 不用 caplog 断言：全量顺序下 test_api_smoke 的 TestClient(app) lifespan 已
    为全局 logger 注册过 handler，caplog.at_level 捕获不到（单独跑文件才过）——
    改为 monkeypatch 记录 logger.info 调用，确定性断言。
    """
    logged: list[str] = []
    monkeypatch.setattr(
        auth_mod.logger, "info",
        lambda *a, **k: logged.append(" ".join(str(x) for x in a)),
    )

    async def _seed():
        async with auth_db() as session:
            session.add(User(
                username="root",
                password_hash=auth_mod.hash_password("fixed-pass-1"),
                role="admin",
            ))
            await session.commit()
    run(_seed())

    assert run(auth_mod.ensure_admin()) is None
    assert any("已存在管理员用户，跳过初始化" in m for m in logged)


# ---------------------------------------------------------------------------
# POST /api/auth/change-password（交付 2）
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bootstrap_admin() -> str:
    """确保 admin 存在并重置其密码为确定性已知值（不删行——删除会撞 guest/审批等
    外键引用 IntegrityError；直接覆写 password_hash 即可拿到可控密码用于登录）。"""
    from app.database import async_session

    password = "seed-pass-123"
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            # 无 admin → 走 ensure_admin 创建（幂等；随后统一覆写哈希）
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password(password)
        await session.commit()
    return password


def test_change_password_api_flow():
    from app.main import app

    with TestClient(app) as client:
        admin_password = client.portal.call(_bootstrap_admin)
        tok = client.post(
            "/api/auth/login", json={"username": "admin", "password": admin_password}
        ).json()["access_token"]

        # 未登录 → 401
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": admin_password, "new_password": "newpass123"},
        )
        assert r.status_code == 401

        # 旧密码错误 → 401
        r = client.post(
            "/api/auth/change-password",
            headers=_auth(tok),
            json={"old_password": "wrong-old-pass", "new_password": "newpass123"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "旧密码错误"

        # 新密码过短 → 422
        r = client.post(
            "/api/auth/change-password",
            headers=_auth(tok),
            json={"old_password": admin_password, "new_password": "123"},
        )
        assert r.status_code == 422

        # 成功改密
        r = client.post(
            "/api/auth/change-password",
            headers=_auth(tok),
            json={"old_password": admin_password, "new_password": "newpass123"},
        )
        assert r.status_code == 200 and r.json() == {"ok": True}

        # 旧密码失效、新密码可登录（token 不改动，仍有效）
        assert client.post(
            "/api/auth/login", json={"username": "admin", "password": admin_password}
        ).status_code == 401
        r = client.post(
            "/api/auth/login", json={"username": "admin", "password": "newpass123"}
        )
        assert r.status_code == 200
        assert client.get("/api/auth/me", headers=_auth(tok)).status_code == 200
