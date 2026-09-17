"""注册接口邀请码爆破限流 + 登录限流复用（Task B2 / Design D3）。

覆盖：
- 窗口内连续无效邀请码达到阈值 → 后续注册请求 429（先判后记，与登录一致）；
- 429 提示统一（「尝试过于频繁，请稍后再试」），不泄露邀请码有效性差异；
- 正常注册（有效邀请码）不受影响 → 200；
- 注册成功后 reset 该 IP 失败计数（防误伤正常用户连续注册，温和策略）；
- 登录限流迁移到同一 RateLimiter 后行为等价：连续错误密码 LIMIT 次 401、
  第 LIMIT+1 次 429、成功登录清除计数（真实阈值，迁移前后行为一致）；
- RateLimiter 单元：窗口过期自动恢复（假时钟）/ reset 清空计数 /
  超限后继续 hit 不无限增长（deque 封顶）。

说明：RateLimiter 单元测试在函数内 import——RED 阶段服务模块尚不存在时，
API 行为测试仍可先跑出断言失败（429 缺失），GREEN 后全部通过。
"""
import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

import app.routers.auth as auth_mod

# 与 auth.py 模块常量 _REGISTER_FAIL_LIMIT 对齐（注册限流阈值语义）。
# 不用 auth_mod 属性引用：RED 阶段该常量尚不存在，字面量可让核心限流
# 断言以「429 缺失」而非 AttributeError 失败。
_EXPECT_REGISTER_LIMIT = 10


def run(coro):
    return asyncio.run(coro)


def _new_code() -> str:
    """生成唯一未使用邀请码（uuid 后缀避免跨测试/跨重跑主键冲突）。"""
    return f"rl-{uuid.uuid4().hex[:12]}"


async def _seed_invite_code(code: str) -> None:
    """注入一个未使用邀请码（表已由 lifespan init_db 创建；已存在则忽略）。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import InviteCode

    async with async_session() as session:
        if await session.scalar(select(InviteCode.code).where(InviteCode.code == code)):
            return
        session.add(InviteCode(code=code))
        await session.commit()


async def _bootstrap_admin_password(password: str = "seed-pass-123") -> str:
    """确保 admin 存在并覆写其密码为确定性已知值（不删行，避免外键冲突）。"""
    from sqlalchemy import select

    from app.database import async_session
    from app.models import User

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password(password)
        await session.commit()
    return password


def _clear_limiters() -> None:
    """清空 auth 模块两个限流器的内部计数（模块级 dict 跨测试存活）。

    测试内直接访问私有 _failures 属 Python 测试惯例；RED 阶段 _register_limiter
    尚不存在时 getattr 兜底跳过。
    """
    for name in ("_register_limiter", "_login_limiter"):
        limiter = getattr(auth_mod, name, None)
        if limiter is not None:
            limiter._failures.clear()


@pytest.fixture()
def api_client():
    """全量 ASGI 客户端（lifespan 自动 init_db → ensure_admin）。"""
    from app.main import app

    with TestClient(app) as client:
        _clear_limiters()
        yield client
        _clear_limiters()


def _register_payload(username: str, invite_code: str, password: str = "pass1234") -> dict:
    return {"username": username, "password": password, "invite_code": invite_code}


# ---------------------------------------------------------------------------
# 1. 注册：窗口内无效邀请码超限 → 429
# ---------------------------------------------------------------------------

def test_register_invalid_invite_429_after_limit(api_client):
    """窗口内连续无效邀请码达到阈值 → 后续注册请求 429（RED：现状恒 422）。"""
    client = api_client
    body = _register_payload("rl-limit-user", "definitely-invalid-code")

    # 阈值内：每次都是 422 邀请码无效/已用
    for i in range(_EXPECT_REGISTER_LIMIT):
        r = client.post("/api/auth/register", json=body)
        assert r.status_code == 422, (i, r.text)
        assert r.json()["detail"] == "邀请码无效或已被使用"

    # 超限：429 且提示统一（不泄露邀请码有效性差异）
    for i in range(2):
        r = client.post("/api/auth/register", json=body)
        assert r.status_code == 429, (i, r.text)
        assert r.json()["detail"] == "尝试过于频繁，请稍后再试"


def test_register_success_unaffected(api_client):
    """有效邀请码正常注册 → 200，限流不误伤正常用户。"""
    client = api_client
    code = _new_code()
    client.portal.call(_seed_invite_code, code)

    r = client.post("/api/auth/register", json=_register_payload("rl-ok-user", code))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "guest"


def test_register_success_resets_ip_failure_count(api_client):
    """邀请码校验成功后 reset 该 IP：计数清零，后续失败从 0 重新计数。

    若未 reset：失败计数已达阈值边缘（阈值-1），成功后的下一次失败会直接
    429；reset 生效则返回 422。此用例在 RED 阶段（无限流）天然通过，
    GREEN 后成为 reset 接线的回归守护。
    """
    client = api_client
    code = _new_code()
    client.portal.call(_seed_invite_code, code)
    invalid = _register_payload("rl-reset-user", "definitely-invalid-code")

    # 失败到阈值边缘（阈值 - 1）
    for _ in range(_EXPECT_REGISTER_LIMIT - 1):
        assert client.post("/api/auth/register", json=invalid).status_code == 422

    # 成功注册 → 应 reset 该 IP 计数
    r = client.post("/api/auth/register", json=_register_payload("rl-reset-user", code))
    assert r.status_code == 200, r.text

    # reset 后：从 0 重新计数 → 422（若未 reset：已达阈值 → 429）
    assert client.post("/api/auth/register", json=invalid).status_code == 422


# ---------------------------------------------------------------------------
# 2. 登录限流迁移等价（Task B2：同一 RateLimiter 实现，不改变阈值语义）
# ---------------------------------------------------------------------------

def test_login_rate_limit_migration_equivalent(api_client):
    """登录限流迁移后行为与迁移前一致：LIMIT 次错误 401 → 第 LIMIT+1 次 429
    → 成功登录清零 → 再失败回到 401（真实阈值 settings.LOGIN_FAIL_LIMIT）。

    迁移前后此行为一致（RED 阶段旧实现同样满足），作为迁移回归守护。
    """
    from app.config import settings

    client = api_client
    pw = client.portal.call(_bootstrap_admin_password)
    # 幂等起点：成功登录一次，清掉共享 DB 可能残留的失败计数
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": pw}
    ).status_code == 200

    # 连续 LOGIN_FAIL_LIMIT 次错误密码 → 401
    for i in range(settings.LOGIN_FAIL_LIMIT):
        r = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
        )
        assert r.status_code == 401, (i, r.text)

    # 第 LIMIT+1 次 → 429，提示统一
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
    )
    assert r.status_code == 429, r.text
    assert "尝试过于频繁" in r.json()["detail"]

    # 正确密码 → 200 且清除限流键
    assert client.post(
        "/api/auth/login", json={"username": "admin", "password": pw}
    ).status_code == 200
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong-pass"}
    )
    assert r.status_code == 401, r.text  # 计数已重置


# ---------------------------------------------------------------------------
# 3. RateLimiter 单元（窗口过期 / reset / 封顶）
# ---------------------------------------------------------------------------

def test_ratelimiter_window_expiry_recovers(monkeypatch):
    """窗口过期后失败计数自动恢复（惰性清理），check 重新放行（假时钟）。"""
    from app.services import rate_limit as rate_limit_mod
    from app.services.rate_limit import RateLimiter

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit_mod, "monotonic", lambda: fake_now[0])

    limiter = RateLimiter(max_failures=2, window_seconds=60)
    limiter.hit("ip-1")
    limiter.hit("ip-1")
    assert limiter.check("ip-1") is False

    fake_now[0] += 61  # 窗口过期
    assert limiter.check("ip-1") is True


def test_ratelimiter_reset_clears_count():
    """reset 清空计数：超限拒绝后 reset 再 check 放行。"""
    from app.services.rate_limit import RateLimiter

    limiter = RateLimiter(max_failures=3, window_seconds=60)
    for _ in range(3):
        limiter.hit("k")
    assert limiter.check("k") is False
    limiter.reset("k")
    assert limiter.check("k") is True


def test_ratelimiter_hit_beyond_limit_stays_capped():
    """超限后继续 hit 不无限增长（deque 以 max_failures 封顶），check 保持拒绝。"""
    from app.services.rate_limit import RateLimiter

    limiter = RateLimiter(max_failures=3, window_seconds=60)
    for _ in range(100):
        limiter.hit("k")
    assert limiter.check("k") is False
    assert len(limiter._failures["k"]) <= 3
