"""Task B3 登录时序侧信道抹平（Design D3）：用户不存在时执行 dummy bcrypt 校验。

背景：用户不存在时旧实现短路跳过 verify_password（bcrypt ~100ms），响应文案虽
统一（「用户名或密码错误」），但响应时间差暴露用户名是否存在（审查 C2）。
本测试断言：
1. 用户不存在时 verify_password 仍被调用一次（dummy 校验发生，抹平时序）；
   —— RED：旧实现不调用，断言失败；GREEN：调用一次。
2. 用户存在但密码错误时行为不变（统一 401，真实校验调用一次）——回归守护。
3. 用户不存在与用户存在但密码错误的响应体一致（统一文案防枚举）。
4. dummy hash 与真实密码哈希同成本（同 rounds）——否则时序差异仍可测。
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import app.routers.auth as auth_mod


def _new_username() -> str:
    """不存在的用户名（uuid 后缀，避免与真实用户冲突/跨测试残留）。"""
    return f"nouser-{uuid.uuid4().hex[:12]}"


async def _bootstrap_admin_password() -> str:
    """确保 admin 存在并覆写其密码为确定性已知值（复制自 test_auth_phase8，
    不删行避免外键冲突；直接覆写 password_hash 拿到可控密码）。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import User

    password = "seed-pass-123"
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password(password)
        await session.commit()
    return password


@pytest.fixture()
def api_client():
    """全量 ASGI 客户端（lifespan 自动 init_db + ensure_admin），清空限流计数。"""
    from app.main import app

    with TestClient(app) as client:
        for name in ("_register_limiter", "_login_limiter"):
            limiter = getattr(auth_mod, name, None)
            if limiter is not None:
                limiter._failures.clear()
        yield client
        for name in ("_register_limiter", "_login_limiter"):
            limiter = getattr(auth_mod, name, None)
            if limiter is not None:
                limiter._failures.clear()


# ---------------------------------------------------------------------------
# 1. 用户不存在 → dummy 校验仍执行（时序抹平核心断言）
# ---------------------------------------------------------------------------

def test_unknown_user_still_runs_dummy_verify(api_client, monkeypatch):
    """用户不存在时 verify_password 仍被调用一次（dummy 校验，抹平时序差异）。

    RED：当前实现 `user is None or not verify_password(...)` 短路跳过校验 →
    spy 未被调用，`assert len(calls) == 1` 失败。
    GREEN：用户不存在分支对固定 dummy hash 执行一次校验 → 调用一次。
    """
    client = api_client
    calls: list = []

    def spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return False  # 恒失败，与「不存在用户」的 401 语义一致

    monkeypatch.setattr(auth_mod, "verify_password", spy)

    r = client.post(
        "/api/auth/login",
        json={"username": _new_username(), "password": "whatever-pass"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"
    # 核心断言：dummy 校验必须发生（RED 阶段失败点）
    assert len(calls) == 1
    # 校验对象必须是合法 bcrypt hash（$2a/$2b/$2y 前缀）
    assert calls[0][1].startswith("$2")


# ---------------------------------------------------------------------------
# 2. 用户存在但密码错误 → 行为不变（回归守护）
# ---------------------------------------------------------------------------

def test_existing_user_wrong_password_unchanged(api_client, monkeypatch):
    """用户存在但密码错误：真实校验调用一次 + 统一 401（成功/失败分支语义不变）。"""
    client = api_client
    client.portal.call(_bootstrap_admin_password)
    calls: list = []

    def spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return False

    monkeypatch.setattr(auth_mod, "verify_password", spy)

    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码错误"
    assert len(calls) == 1  # 真实路径校验恰一次，不额外触发 dummy


# ---------------------------------------------------------------------------
# 3. 用户不存在 vs 密码错误的响应体一致（统一文案防枚举）
# ---------------------------------------------------------------------------

def test_unknown_vs_wrong_password_response_identical(api_client):
    """两种失败场景响应体完全一致（真实 bcrypt，端到端验证统一文案）。"""
    client = api_client
    client.portal.call(_bootstrap_admin_password)

    r_unknown = client.post(
        "/api/auth/login",
        json={"username": _new_username(), "password": "x-pass-unknown"},
    )
    r_wrong = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "x-pass-wrong"},
    )
    assert r_unknown.status_code == 401 and r_wrong.status_code == 401
    assert r_unknown.json() == r_wrong.json()
    assert r_wrong.json() == {"detail": "用户名或密码错误"}


# ---------------------------------------------------------------------------
# 4. dummy hash 与真实密码哈希同成本（边界注意：否则时序差异仍可测）
# ---------------------------------------------------------------------------

def test_dummy_hash_same_cost_as_real():
    """dummy hash 与真实密码哈希同 rounds（bcrypt 成本参数），防止成本漂移。

    bcrypt hash 格式 `$2b$<cost>$<salt+hash>`，`split("$")[2]` 即成本。
    RED：模块尚无 `_DUMMY_PASSWORD_HASH` 常量 → `assert dummy is not None` 失败。
    """
    dummy = getattr(auth_mod, "_DUMMY_PASSWORD_HASH", None)
    assert dummy is not None, "RED 阶段：dummy 常量尚未实现"
    assert dummy.startswith("$2")

    real = auth_mod.hash_password("probe-password")
    assert dummy.split("$")[2] == real.split("$")[2]
