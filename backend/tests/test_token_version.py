"""Task B1：修改密码吊销既有令牌（token_version）。

覆盖（tdd_mode: tdd，先 RED 后 GREEN）：
- 改密前签发的 token，改密后请求受保护端点 → 401（核心需求，C3 审查：7 天
  有效期内旧 token 持续有效 → 应吊销）
- 未改密的用户 token 仍有效（回归锚点）
- 改密后重新登录签发的新 token 有效
- 存量无 `ver` 字段的 token 兼容：用户未改密前仍有效（视为版本 0），改密后失效
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from jose import jwt as jose_jwt
from sqlalchemy import select

from app.config import _JWT_SECRET, settings
from app.main import app
from app.models import User
from app.routers import auth as auth_mod

_TEST_USER = "tokver_user"
_TEST_PASS = "tokver-pass-123"
_NEW_PASS = "tokver-new-456"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bootstrap_user() -> str:
    """幂等种子：确保测试用户存在，密码与 token_version 重置为确定性初始值。

    覆写 password_hash 并重置 token_version=0——全量 pytest 共享同一 app 数据库，
    避免前序用例改密副作用污染断言。
    """
    from app.database import async_session

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.username == _TEST_USER))
        if user is None:
            user = User(
                username=_TEST_USER,
                password_hash=auth_mod.hash_password(_TEST_PASS),
                role="admin",
            )
            session.add(user)
        user.password_hash = auth_mod.hash_password(_TEST_PASS)
        user.token_version = 0
        await session.commit()
    return _TEST_PASS


async def _user_identity() -> tuple[int, str]:
    """返回测试用户 (id, role)，供构造存量 token 使用。"""
    from app.database import async_session

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.username == _TEST_USER))
        assert user is not None
        return user.id, user.role


def _legacy_token(user_id: int, role: str) -> str:
    """构造无 `ver` 字段的存量 token（模拟升级前签发，payload 不含版本号）。"""
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": str(user_id), "role": role, "exp": expire}
    return jose_jwt.encode(payload, _JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _login(client: TestClient, password: str) -> str:
    resp = client.post(
        "/api/auth/login", json={"username": _TEST_USER, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _change_password(client: TestClient, token: str, old: str, new: str):
    resp = client.post(
        "/api/auth/change-password",
        headers=_auth(token),
        json={"old_password": old, "new_password": new},
    )
    assert resp.status_code == 200, resp.text


def test_change_password_revokes_old_token():
    """改密前签发的 token，改密后访问受保护端点 → 401（核心需求）。"""
    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_user)
        tok = _login(client, password)
        # 前置：改密前 token 有效
        assert client.get("/api/auth/me", headers=_auth(tok)).status_code == 200

        _change_password(client, tok, password, _NEW_PASS)

        # 核心断言：改密后旧 token 被吊销
        assert client.get("/api/auth/me", headers=_auth(tok)).status_code == 401


def test_unrevoked_token_still_valid():
    """未改密用户的 token 仍有效（回归锚点）。"""
    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_user)
        tok = _login(client, password)
        r = client.get("/api/auth/me", headers=_auth(tok))
        assert r.status_code == 200
        assert r.json()["username"] == _TEST_USER


def test_new_login_after_password_change_works():
    """改密后重新登录签发的新 token 有效。"""
    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_user)
        tok_old = _login(client, password)
        _change_password(client, tok_old, password, _NEW_PASS)
        assert client.get("/api/auth/me", headers=_auth(tok_old)).status_code == 401

        tok_new = _login(client, _NEW_PASS)
        assert client.get("/api/auth/me", headers=_auth(tok_new)).status_code == 200


def test_legacy_token_without_ver_compatible():
    """存量无 ver 字段的 token：未改密前仍有效（视为版本 0）；改密后失效。"""
    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_user)
        uid, role = client.portal.call(_user_identity)
        legacy_tok = _legacy_token(uid, role)

        # 兼容：无 ver → 视为版本 0，用户未改密 → 仍有效
        assert client.get("/api/auth/me", headers=_auth(legacy_tok)).status_code == 200

        tok = _login(client, password)
        _change_password(client, tok, password, _NEW_PASS)

        # 改密后存量 token 同样失效
        assert client.get("/api/auth/me", headers=_auth(legacy_tok)).status_code == 401
