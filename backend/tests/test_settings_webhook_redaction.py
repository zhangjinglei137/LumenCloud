"""Task B7 webhook 回调鉴权密钥遮蔽单测（Design D4 附属 / 审查 C7）。

覆盖：
- GET /api/settings 对 internal_aria2_webhook_secret / internal_nastools_webhook_token
  返回 "***" 占位（不回显明文）；
- PATCH 不含（前端留空不提交的）webhook 键时不覆盖既有真实值；
- 遮蔽仅为 GET 展示层：config_store 内部真实值保持不变（notify.py /
  nastools_notify.py 回调鉴权读取不受影响）。

依赖共享 app 单例（TestClient(app)，lifespan 自动跑 init_db → ensure_admin）。
各测试自备确定性 admin 密码（覆写 password_hash，不删行，避免外键引用冲突）。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.services import config_store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """每个测试前后重置 config_store 模块级缓存（仿 test_security_hardening.py），
    防止 TestClient lifespan 的 load_from_db 污染跨测试状态。"""
    saved = (config_store._cache, config_store._loaded)
    config_store._cache = {}
    config_store._loaded = False
    yield
    config_store._cache, config_store._loaded = saved


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _bootstrap_admin() -> str:
    """确保 admin 存在并重置密码为确定性已知值（供登录断言）。"""
    from app.database import async_session
    from app.models import User
    import app.routers.auth as auth_mod

    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password("seed-pass-123")
        await session.commit()
    return "seed-pass-123"


def _login_admin(client: TestClient) -> str:
    pw = client.portal.call(_bootstrap_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": pw})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


_WEBHOOK_KEYS = ("internal_aria2_webhook_secret", "internal_nastools_webhook_token")


def _patch_webhook_values(client: TestClient, tok: str) -> dict:
    """PATCH 写入确定的 webhook 真实值并断言成功，返回键值映射供后续断言。"""
    values = {
        "internal_aria2_webhook_secret": "real-secret-abc123",
        "internal_nastools_webhook_token": "real-token-xyz789",
    }
    r = client.patch("/api/settings", json=dict(values), headers=_auth(tok))
    assert r.status_code == 200, r.text
    return values


# ---------------------------------------------------------------------------
# 1. GET 遮蔽：webhook 密钥回 "***" 占位，响应体不含明文
# ---------------------------------------------------------------------------

def test_settings_get_masks_webhook_keys():
    with TestClient(app) as client:
        tok = _login_admin(client)
        values = _patch_webhook_values(client, tok)

        r = client.get("/api/settings", headers=_auth(tok))
        assert r.status_code == 200, r.text
        cfg = r.json()["system_config"]
        for key in _WEBHOOK_KEYS:
            assert cfg.get(key) == "***", f"{key} 应以 *** 占位（当前 {cfg.get(key)!r}）"
        # 响应全文（含 config/services 等字段）不得出现明文
        for secret in values.values():
            assert secret not in r.text, f"GET /api/settings 泄露明文 {secret!r}"


# ---------------------------------------------------------------------------
# 2. PATCH 留空（前端 dirtyCredKeys：不提交未修改字段）不覆盖既有值
# ---------------------------------------------------------------------------

def test_patch_without_webhook_keys_keeps_existing_values():
    with TestClient(app) as client:
        tok = _login_admin(client)
        values = _patch_webhook_values(client, tok)

        # 模拟前端只保存其他键、webhook 键留空不提交：PATCH 不含 webhook 键
        r = client.patch("/api/settings", json={"quark_quota_gb": "55"}, headers=_auth(tok))
        assert r.status_code == 200, r.text

        # 内部真实值不变（遮蔽只是 GET 展示层，config_store 读写不受影响）
        for key, expected in values.items():
            assert config_store.get(key) == expected, f"{key} 被意外覆盖：{config_store.get(key)!r}"

        # 还原配额配置，避免残留污染后续共享库的容量/告警测试（同 test_api_smoke 惯例）
        r = client.patch("/api/settings", json={"quark_quota_gb": "10"}, headers=_auth(tok))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 3. 遮蔽不影响内部读写：GET 遮蔽后 config_store.get 仍为真实值
# ---------------------------------------------------------------------------

def test_masking_does_not_affect_internal_read():
    with TestClient(app) as client:
        tok = _login_admin(client)
        values = _patch_webhook_values(client, tok)

        # 先 GET（触发遮蔽展示），再确认进程内配置缓存读到的是真实值
        r = client.get("/api/settings", headers=_auth(tok))
        assert r.status_code == 200
        for key, expected in values.items():
            assert config_store.get(key) == expected


# ---------------------------------------------------------------------------
# 4.1：download_queue_paused 为 PATCH 白名单键（设置页队列暂停开关）
# ---------------------------------------------------------------------------

def test_patch_download_queue_paused_returns_200():
    """PATCH download_queue_paused → 200 且立即生效（config_store 缓存刷新）。

    fix-audit-issues 4.1（审查 D3）：该键此前不在 _WHITELIST_EXACT，设置页
    队列暂停开关保存必 422。期望：
    - PATCH 保存返回 200，且 config_store 缓存刷新后立即读到新值（保存即生效）；
    - 该键是业务开关（transfer.py / queue.py 读取暂停下载队列），非服务凭据——
      不进 _EDITABLE_KEYS 凭据表单（避免前端渲染成凭据文本框）。
    """
    with TestClient(app) as client:
        tok = _login_admin(client)
        r = client.patch(
            "/api/settings",
            json={"download_queue_paused": "true"},
            headers=_auth(tok),
        )
        assert r.status_code == 200, r.text
        # 保存即生效：进程内配置缓存已刷新
        assert config_store.get("download_queue_paused") == "true"

        # 白名单契约：可写但非凭据表单（editable_keys 不含该键）
        r = client.get("/api/settings", headers=_auth(tok))
        assert r.status_code == 200
        assert "download_queue_paused" not in r.json()["editable_keys"]

        # 还原为 false，避免残留影响共享库后续测试
        r = client.patch(
            "/api/settings",
            json={"download_queue_paused": "false"},
            headers=_auth(tok),
        )
        assert r.status_code == 200, r.text
