"""Task 6：GET /queue/download/state 降为登录可读（队列页访客不再误报权限）。

线上问题：guest 打开任务队列页面，前端 onMounted 调用 GET /api/queue/download/state
（原 admin-only）→ 403 → 全局拦截器弹「需要管理员权限」。

权限契约（§8.2 例外——暂停开关与在途任务数是展示数据，非敏感）：
- guest / admin 读 GET /api/queue/download/state → 200
- 未登录（无 token / cookie）→ 401
- 写操作 POST /api/queue/download/pause 维持 admin-only → guest 403

实现说明（Fix Round 1）：不再采用 test_api_smoke 的「删 admin 重建拿初始密码」模式
（该模式与 smoke 争抢共享 app 库里的 admin 用户，且 invite_codes.created_by FK 残留
引用 admin 会让后执行的模块 delete admin 时抛 IntegrityError，全量回归必红）。改为
直接 seed admin/guest 用户（uuid 后缀用户名防撞 UNIQUE）并用 create_access_token
签发 token——不删 admin、不建邀请码、不依赖 HTTP 登录流程，与 smoke 零互斥。

HTTP 层测试（TestClient + Bearer token），权限判定由 Depends(get_current_admin)/
get_current_user 真实执行；显式 Bearer 头优先于 cookie（deps header 优先）。
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


async def _seed_users() -> dict:
    """seed admin/guest 用户并签发 token（commit 前签发避免 ORM expire 重查）。

    不删除/重建 admin、不写 invite_codes——与 test_api_smoke 共享同一 app 库时
    无 FK 残留、无争抢；uuid 后缀用户名避开 users.username UNIQUE 冲突。
    """
    from app.database import async_session
    from app.models import User
    from app.routers.auth import create_access_token, hash_password

    async with async_session() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:10]}",
            password_hash=hash_password("x"),
            role="admin",
        )
        guest = User(
            username=f"guest_{uuid.uuid4().hex[:10]}",
            password_hash=hash_password("x"),
            role="guest",
        )
        session.add_all([admin, guest])
        await session.flush()
        toks = {
            "admin_tok": create_access_token(admin),
            "guest_tok": create_access_token(guest),
        }
        await session.commit()
        return toks


@pytest.fixture(scope="module")
def users():
    """module 级共享：TestClient + admin/guest token（seed 用户直接签发，无删建 admin 互斥）。"""
    with TestClient(app) as client:
        toks = client.portal.call(_seed_users)
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


def test_anonymous_cannot_read_download_state(users):
    """未登录（无 token、无 cookie）→ 401（降权不改变匿名语义）。"""
    client = users["client"]
    client.cookies.clear()  # 隔离 cookie 兜底通道
    r = client.get("/api/queue/download/state")
    assert r.status_code == 401, r.text


def test_guest_cannot_pause_download_queue(users):
    """写操作保持 admin-only：guest POST /api/queue/download/pause → 403。"""
    r = users["client"].post("/api/queue/download/pause", headers=_auth(users["guest_tok"]))
    assert r.status_code == 403, r.text


def test_admin_can_pause_download_queue(users):
    """admin POST pause → 200；finally 兜底 resume 还原共享库暂停开关（防断言失败残留）。"""
    client = users["client"]
    tok = users["admin_tok"]
    try:
        r = client.post("/api/queue/download/pause", headers=_auth(tok))
        assert r.status_code == 200, r.text
    finally:
        client.post("/api/queue/download/resume", headers=_auth(tok))