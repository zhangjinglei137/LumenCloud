"""Q11 用户管理 API 单测（admin：列表 / 改角色 / 删除）。

保护规则覆盖：
- 列表：字段齐全、created_at desc + id desc 排序
- PATCH：404 / 非法 role（pydantic → 422）/ 自改 409 / 幂等 ok / 正常流转
- DELETE：404 / 自删 409 / 关联引用（watch_request / notification）409 / 无引用成功

in-memory SQLite（参照 test_fix_online：Base.metadata.create_all + StaticPool），
直接调用 admin 路由函数绕过 Depends（admin=SimpleNamespace(id=1, role="admin",
username="boss")，与 seed 的第一个用户「boss」的 id=1 对齐）。
"""
import os
import tempfile

# 测试环境隔离（收敛修复，同 test_alist_pagination.py）：本文件按 pytest 字母序
# 最先被收集，若不在导入任何 app 模块前清空外部服务凭据，app.config.settings 会以
# 项目根 .env 的真实凭据实例化并缓存，导致其后 test_api_smoke 顶部 os.environ
# 覆盖失效、批准流程触发真实 cloudSaver 联网入队（确定性失败的根因，第 2 批记录）。
_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_admin_users_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
for _K in (
    "TMDB_API_KEY", "TMDB_PROXY", "CLOUDSAVER_BASE_URL", "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD", "EMBY_BASE_URL", "EMBY_API_KEY", "ALIST_BASE_URL",
    "ALIST_TOKEN", "ARIA2_RPC_URL", "ARIA2_TOKEN", "NASTOOLS_BASE_URL", "PUSHPLUS_TOKEN",
):
    os.environ[_K] = ""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Notification, User, WatchRequest
from app.routers.admin import RoleUpdate, delete_user, list_users, update_user_role


def run(coro):
    return asyncio.run(coro)


# 当前登录用户（与 seed 第一个用户 boss 的 id=1 对齐）
ADMIN = SimpleNamespace(id=1, role="admin", username="boss")


@pytest.fixture()
def _db_maker():
    """隔离的 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构。"""
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
    yield maker
    run(engine.dispose())


def _seed_users(_db_maker, *users):
    """按 (username, role) 列表 seed 用户，返回 {username: id}（首个 boss 为 id=1）。"""
    async def _seed():
        async with _db_maker() as session:
            for username, role in users:
                session.add(User(username=username, password_hash="x", role=role))
            await session.commit()

    async def _ids():
        async with _db_maker() as session:
            rows = (
                await session.execute(select(User.username, User.id))
            ).all()
            return {u: i for u, i in rows}

    run(_seed())
    return run(_ids())


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------

def test_list_users_ordering_and_fields(_db_maker):
    _seed_users(_db_maker, ("boss", "admin"), ("alice", "guest"))

    async def _case():
        async with _db_maker() as session:
            return await list_users(admin=ADMIN, session=session)

    rows = run(_case())
    assert len(rows) == 2
    by_name = {r["username"]: r for r in rows}
    assert by_name["boss"]["role"] == "admin"
    assert by_name["alice"]["role"] == "guest"
    assert by_name["alice"]["invite_code"] is None
    assert "created_at" in by_name["alice"] and by_name["alice"]["created_at"] is not None
    assert by_name["alice"]["id"] == by_name["alice"]["id"]
    # 排序 created_at desc、同秒时 id desc（seed 同瞬，此处验证 id desc 兜底生效）
    assert rows[0]["id"] > rows[1]["id"]


# ---------------------------------------------------------------------------
# PATCH 改角色
# ---------------------------------------------------------------------------

def test_patch_role_guest_to_admin_persists(_db_maker):
    ids = _seed_users(_db_maker, ("boss", "admin"), ("alice", "guest"))

    async def _case():
        async with _db_maker() as session:
            res = await update_user_role(
                payload=RoleUpdate(role="admin"), user_id=ids["alice"],
                admin=ADMIN, session=session,
            )
            assert res == {"ok": True}
            saved = await session.get(User, ids["alice"])
            assert saved.role == "admin"

    run(_case())


def test_patch_role_idempotent(_db_maker):
    """目标已是该 role → 幂等直接 ok（不报 409）。"""
    ids = _seed_users(_db_maker, ("boss", "admin"), ("alice", "guest"))

    async def _case():
        async with _db_maker() as session:
            res = await update_user_role(
                payload=RoleUpdate(role="guest"), user_id=ids["alice"],
                admin=ADMIN, session=session,
            )
            assert res == {"ok": True}

    run(_case())


def test_patch_self_409(_db_maker):
    _seed_users(_db_maker, ("boss", "admin"))

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await update_user_role(
                    payload=RoleUpdate(role="guest"), user_id=ADMIN.id,
                    admin=ADMIN, session=session,
                )
            assert ei.value.status_code == 409
            assert "自己" in ei.value.detail

    run(_case())


def test_patch_404(_db_maker):
    _seed_users(_db_maker, ("boss", "admin"))

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await update_user_role(
                    payload=RoleUpdate(role="admin"), user_id=9999,
                    admin=ADMIN, session=session,
                )
            assert ei.value.status_code == 404

    run(_case())


def test_patch_invalid_role_422():
    """非法 role → pydantic ValidationError（FastAPI 层映射为 HTTP 422）。"""
    with pytest.raises(ValidationError):
        RoleUpdate(role="superuser")


def test_patch_role_flow_admin_transfer(_db_maker):
    """正常流转：guest→admin 成功；另一 admin/guest 互转成功；重复请求幂等。"""
    ids = _seed_users(
        _db_maker, ("boss", "admin"), ("alice", "admin"), ("bob", "guest")
    )

    async def _case():
        async with _db_maker() as session:
            # bob guest→admin（admin 总数 3）
            res = await update_user_role(
                payload=RoleUpdate(role="admin"), user_id=ids["bob"],
                admin=ADMIN, session=session,
            )
            assert res == {"ok": True}
            # bob 已是 admin，再 PATCH admin → 幂等 ok
            res = await update_user_role(
                payload=RoleUpdate(role="admin"), user_id=ids["bob"],
                admin=ADMIN, session=session,
            )
            assert res == {"ok": True}
            # alice admin→guest（仍剩 boss+bob 两个 admin，允许）
            res = await update_user_role(
                payload=RoleUpdate(role="guest"), user_id=ids["alice"],
                admin=ADMIN, session=session,
            )
            assert res == {"ok": True}
            saved = await session.get(User, ids["bob"])
            assert saved.role == "admin"
            saved = await session.get(User, ids["alice"])
            assert saved.role == "guest"

    run(_case())


# ---------------------------------------------------------------------------
# DELETE 删除
# ---------------------------------------------------------------------------

def test_delete_self_409(_db_maker):
    _seed_users(_db_maker, ("boss", "admin"))

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await delete_user(user_id=ADMIN.id, admin=ADMIN, session=session)
            assert ei.value.status_code == 409

    run(_case())


def test_delete_guest_ok_and_404_boundary(_db_maker):
    """只有自己一个 admin：删 guest 成功；再删同一 id → 404（边界）。"""
    ids = _seed_users(
        _db_maker, ("boss", "admin"), ("alice", "guest"), ("bob", "guest")
    )

    async def _case():
        async with _db_maker() as session:
            res = await delete_user(user_id=ids["alice"], admin=ADMIN, session=session)
            assert res == {"ok": True}
            with pytest.raises(HTTPException) as ei:
                await delete_user(user_id=ids["alice"], admin=ADMIN, session=session)
            assert ei.value.status_code == 404

    run(_case())


def test_delete_other_admin_ok(_db_maker):
    """自己 + 另一 admin（总数 2）：删另一 admin 成功（兜底保护不误拦）。"""
    ids = _seed_users(_db_maker, ("boss", "admin"), ("alice", "admin"))

    async def _case():
        async with _db_maker() as session:
            res = await delete_user(user_id=ids["alice"], admin=ADMIN, session=session)
            assert res == {"ok": True}

    run(_case())


def test_delete_watch_request_ref_409(_db_maker):
    ids = _seed_users(_db_maker, ("boss", "admin"), ("alice", "guest"))

    async def _seed_ref():
        async with _db_maker() as session:
            session.add(
                WatchRequest(requested_by=ids["alice"], title="想看 X", status="pending")
            )
            await session.commit()

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await delete_user(user_id=ids["alice"], admin=ADMIN, session=session)
            assert ei.value.status_code == 409
            assert "关联记录" in ei.value.detail

    run(_seed_ref())
    run(_case())


def test_delete_notification_ref_409(_db_maker):
    ids = _seed_users(_db_maker, ("boss", "admin"), ("alice", "guest"))

    async def _seed_ref():
        async with _db_maker() as session:
            session.add(
                Notification(
                    recipient=ids["alice"], event_type="download_complete", title="下载完成"
                )
            )
            await session.commit()

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await delete_user(user_id=ids["alice"], admin=ADMIN, session=session)
            assert ei.value.status_code == 409

    run(_seed_ref())
    run(_case())