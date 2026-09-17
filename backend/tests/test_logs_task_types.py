"""Task C10（Design D11）：运行日志任务类型枚举后端下发测试。

后端新增 GET /api/logs/task-types（admin-only，与 /api/logs 查询鉴权一致），
返回当前实际任务类型列表（静态常量，与 backend/app/tasks/*.py 的
record_task_run 调用点一一对应）。

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向独立临时目录（同 test_api_smoke）；
lifespan（init_db → recover_on_boot → ensure_admin → scheduler）由 TestClient
上下文自动触发。
"""
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_logs_task_types_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

# 期望完整集合：静态常量，与 backend/app/tasks/*.py record_task_run 写入值一致
# （scan.py: scan_media/scan_all_media；transfer.py: transfer；cleanup.py:
# cleanup/prune_history；capacity_alert.py: capacity_alert；recovery.py: recover；
# nastools_sync.py: sync_nastools；notification_scan.py: notify）。
_EXPECTED_TYPES = [
    "scan_media",
    "scan_all_media",
    "transfer",
    "cleanup",
    "prune_history",
    "capacity_alert",
    "recover",
    "sync_nastools",
    "notify",
]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _reset_admin_password() -> None:
    """把 admin 密码改写为已知值（不删除行，避免共享库 FK 冲突）。

    与 test_api_smoke 的 _recreate_admin（删除后重建）不同：本项目测试共享进程级
    engine/SQLite（后导入模块的 LUMENCLOUD_DATA_DIR 对已缓存的 app.main 失效），
    若 smoke 先跑完，其遗留数据（通知/审批流等）可能外键引用 admin 行，删除会触发
    FOREIGN KEY constraint failed；改写密码无此问题。admin 由 lifespan ensure_admin
    保证存在。
    """
    from sqlalchemy import update

    from app.database import async_session
    from app.models import User
    from app.routers.auth import hash_password

    async with async_session() as session:
        await session.execute(
            update(User)
            .where(User.role == "admin")
            .values(password_hash=hash_password("C10AdminPass!234"))
        )
        await session.commit()


def _login_admin(client: TestClient) -> str:
    """改写 admin 密码后登录，返回 admin token。"""
    client.portal.call(_reset_admin_password)
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "C10AdminPass!234"}
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_task_types_admin_returns_full_type_list():
    """admin 访问 /api/logs/task-types → 200，返回全部任务类型（含当前实际类型）。"""
    with TestClient(app) as client:
        admin_tok = _login_admin(client)

        r = client.get("/api/logs/task-types", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, dict) and "types" in body
        assert body["types"] == _EXPECTED_TYPES


async def _create_guest() -> None:
    """直接插入 guest 用户（绕过邀请码注册，避免产生 created_by=admin 的邀请码行）。

    本项目测试共享进程级 engine/SQLite：邀请码注册会写入 invite_codes.created_by
    （=admin，无 ondelete），残留会破坏后续 test_api_smoke 的 _recreate_admin
    （删除 admin 触发 FOREIGN KEY constraint failed）。直接插库只写 users 行，
    无外键引用，与其它测试互不影响；幂等（已存在则跳过）。
    """
    from sqlalchemy import select

    from app.database import async_session
    from app.models import User
    from app.routers.auth import hash_password

    async with async_session() as session:
        exists = (
            await session.execute(select(User).where(User.username == "c10guest"))
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                User(
                    username="c10guest",
                    password_hash=hash_password("pass1234"),
                    role="guest",
                )
            )
            await session.commit()


def test_task_types_admin_only():
    """task-types 端点鉴权与 logs 查询一致：未登录 401、guest 403、admin 200。"""
    with TestClient(app) as client:
        # 无效 token → 401
        assert client.get("/api/logs/task-types", headers=_auth("bad.token.here")).status_code == 401

        admin_tok = _login_admin(client)

        # guest 登录（直接插库创建，见 _create_guest 注释）
        client.portal.call(_create_guest)
        r = client.post("/api/auth/login", json={"username": "c10guest", "password": "pass1234"})
        assert r.status_code == 200, r.text
        guest_tok = r.json()["access_token"]

        # guest → 403（仅 admin，与 /api/logs 查询一致）
        assert client.get("/api/logs/task-types", headers=_auth(guest_tok)).status_code == 403
        # admin → 200
        assert client.get("/api/logs/task-types", headers=_auth(admin_tok)).status_code == 200
