"""Task 6：GET /queue/download/state 降为登录可读（队列页访客不再误报权限）。

线上问题：guest 打开任务队列页面，前端 onMounted 调用 GET /api/queue/download/state
（原 admin-only）→ 403 → 全局拦截器弹「需要管理员权限」。

权限契约（§8.2 例外——暂停开关与在途任务数是展示数据，非敏感）：
- guest / admin 读 GET /api/queue/download/state → 200
- 写操作 POST /api/queue/download/pause 维持 admin-only → guest 403

HTTP 层测试（TestClient + 真实登录），参考 test_api_smoke.py：动态 admin 初始化
（Phase 8 随机初始密码）+ 邀请码注册 guest + 登录。队列 state/pause 不触发外部
服务调用，无需 monkeypatch；guest 用户名带 uuid 后缀防跨测试重名（共享库幂等）。
"""
import os
import tempfile
import uuid

import pytest

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_queue_auth_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["JWT_SECRET"] = "queue-auth-secret-12345"
os.environ["INIT_ADMIN_USERNAME"] = "admin"
# 隔离外部服务凭据（测试不发起外部调用，置空仅为避免引用项目根 .env 的真实值）
for _K in (
    "TMDB_API_KEY", "TMDB_PROXY", "CLOUDSAVER_BASE_URL", "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD", "EMBY_BASE_URL", "EMBY_API_KEY", "ALIST_BASE_URL",
    "ALIST_TOKEN", "ARIA2_RPC_URL", "ARIA2_TOKEN", "NASTOOLS_BASE_URL", "PUSHPLUS_TOKEN",
):
    os.environ[_K] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


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


def _make_users(client: TestClient) -> dict:
    """准备 admin / guest 两个身份 → {admin_tok, guest_tok}（client cookie 最终停留 guest）。"""
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    admin_tok = r.json()["access_token"]

    r = client.post("/api/admin/invites", json={"count": 1}, headers=_auth(admin_tok))
    assert r.status_code == 200, r.text
    code = r.json()["codes"][0]
    guest_name = f"guest_{uuid.uuid4().hex[:10]}"
    r = client.post(
        "/api/auth/register",
        json={"username": guest_name, "password": "pass1234", "invite_code": code},
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "guest"

    r = client.post("/api/auth/login", json={"username": guest_name, "password": "pass1234"})
    assert r.status_code == 200, r.text
    return {"admin_tok": admin_tok, "guest_tok": r.json()["access_token"]}


@pytest.fixture(scope="module")
def users():
    """module 级共享：TestClient + admin/guest token。

    _recreate_admin 只能删建一次（invite_codes.created_by 外键引用 admin，重复
    删除会触发 FK 约束失败）——此处在干净库上重建 admin、注册 guest，供全部
    用例共享；显式 Bearer 头优先于 cookie（deps header 优先），测试互不干扰。
    """
    with TestClient(app) as client:
        toks = _make_users(client)
        yield {"client": client, **toks}


def test_guest_can_read_download_state(users):
    """guest 读 GET /api/queue/download/state → 200（暂停开关 + 在途任务数，展示数据）。"""
    r = users["client"].get("/api/queue/download/state", headers=_auth(users["guest_tok"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert "paused" in body and "in_flight" in body


def test_admin_can_read_download_state(users):
    """admin 读 GET /api/queue/download/state → 200（回归锁定）。"""
    r = users["client"].get("/api/queue/download/state", headers=_auth(users["admin_tok"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)
    assert "paused" in body and "in_flight" in body


def test_guest_cannot_pause_download_queue(users):
    """写操作保持 admin-only：guest POST /api/queue/download/pause → 403。"""
    r = users["client"].post("/api/queue/download/pause", headers=_auth(users["guest_tok"]))
    assert r.status_code == 403, r.text


def test_admin_can_pause_download_queue(users):
    """admin POST pause → 200；随后 resume 还原共享库开关，避免残留污染其他测试。"""
    r = users["client"].post("/api/queue/download/pause", headers=_auth(users["admin_tok"]))
    assert r.status_code == 200, r.text
    r = users["client"].post("/api/queue/download/resume", headers=_auth(users["admin_tok"]))
    assert r.status_code == 200, r.text