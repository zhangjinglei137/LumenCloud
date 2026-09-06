"""Q10 + Q6（组 B 后端）：列表接口 LEFT JOIN users 返回 username 单测。

覆盖三个接口（直接调路由函数绕过 Depends 注入）：
- list_approvals   → requested_by_username（保留 requested_by id）
- list_notifications → recipient_username（recipient=NULL 全体消息为 None）
- list_invites     → used_by_username（保留 used_by id）

数据库用隔离的 in-memory SQLite（Base.metadata.create_all，参照
test_fix_online 的 _db_maker 模式）；LEFT JOIN 兜底（对不上不报错）由
outer join 天然保证。
"""
import asyncio
import types

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import InviteCode, Notification, User, WatchRequest
from app.routers.admin import list_invites
from app.routers.approvals import list_approvals
from app.routers.notifications import list_notifications

ADMIN = types.SimpleNamespace(id=1, role="admin")


def run(coro):
    return asyncio.run(coro)


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


def _seed_user(_db_maker, *, username: str) -> int:
    """插入一个用户并返回其自增 id。"""

    async def _seed():
        async with _db_maker() as session:
            u = User(username=username, password_hash="x", role="guest")
            session.add(u)
            await session.flush()
            uid = u.id
            await session.commit()
            return uid

    return run(_seed())


# ---------------------------------------------------------------------------
# 1) 有关联用户：三个接口 *_username 均返回用户名（id 字段保留）
# ---------------------------------------------------------------------------

def test_join_username_populated(_db_maker):
    uid = _seed_user(_db_maker, username="alice")

    async def _seed():
        async with _db_maker() as session:
            session.add(WatchRequest(requested_by=uid, title="想看A", status="pending"))
            session.add(InviteCode(code="INVITE1", used_by=uid))
            session.add(Notification(recipient=uid, event_type="download_complete",
                                     title="完成", body="b"))
            await session.commit()

    run(_seed())

    async def _case():
        async with _db_maker() as session:
            # 审批列表：admin 看全部，requested_by(id) 保留 + username 附加
            wrs = await list_approvals(user=ADMIN, session=session)
            assert wrs[0]["requested_by"] == uid
            assert wrs[0]["requested_by_username"] == "alice"

            # 邀请码列表：used_by(id) 保留 + username 附加
            invs = await list_invites(admin=ADMIN, session=session)
            assert invs[0]["used_by"] == uid
            assert invs[0]["used_by_username"] == "alice"

            # 通知列表：收件人为本人，username 附加；unread_count 语义不变
            notifs = await list_notifications(user=ADMIN, session=session)
            assert notifs["items"][0]["recipient_username"] == "alice"
            assert notifs["unread_count"] == 1

    run(_case())


# ---------------------------------------------------------------------------
# 2) 无关联用户（requested_by/used_by/recipient 指向不存在的 user id）：
#    *_username 为 None 且不报错（LEFT JOIN 兜底）
# ---------------------------------------------------------------------------

def test_join_username_null_for_missing_user(_db_maker):
    # 不插任何 User；用不存在的 id 关联
    async def _seed():
        async with _db_maker() as session:
            session.add(WatchRequest(requested_by=999, title="孤儿审批", status="pending"))
            session.add(InviteCode(code="ORPHAN1", used_by=999))
            # 通知 scope 要求 recipient==当前用户 id（此处 user.id=1 但无该 User）
            session.add(Notification(recipient=1, event_type="approval_pending",
                                     title="孤儿通知", body="b"))
            await session.commit()

    run(_seed())

    async def _case():
        async with _db_maker() as session:
            wrs = await list_approvals(user=ADMIN, session=session)
            assert wrs[0]["requested_by"] == 999
            assert wrs[0]["requested_by_username"] is None

            invs = await list_invites(admin=ADMIN, session=session)
            assert invs[0]["used_by"] == 999
            assert invs[0]["used_by_username"] is None

            notifs = await list_notifications(user=ADMIN, session=session)
            assert notifs["items"][0]["recipient_username"] is None
            assert notifs["unread_count"] == 1

    run(_case())


# ---------------------------------------------------------------------------
# 3) notifications 范围语义不变：recipient=NULL 的全体消息保留在列表
#    （recipient_username 为 None）
# ---------------------------------------------------------------------------

def test_notifications_broadcast_scope_kept(_db_maker):
    async def _seed():
        async with _db_maker() as session:
            # 全体消息（recipient=NULL）+ 已读一条，验证范围与 unread_count 不变
            session.add(Notification(recipient=None, event_type="approval_pending",
                                     title="全体通知", body="all", is_read=False))
            session.add(Notification(recipient=1, event_type="download_complete",
                                     title="本人已读", body="me", is_read=True))
            await session.commit()

    run(_seed())

    async def _case():
        async with _db_maker() as session:
            notifs = await list_notifications(user=ADMIN, session=session)
            titles = [it["title"] for it in notifs["items"]]
            # 全体消息保留在列表（scope 语义不变）
            assert "全体通知" in titles
            broadcast = next(it for it in notifs["items"] if it["title"] == "全体通知")
            assert broadcast["recipient_username"] is None
            # unread_count 只计未读（未读优先排序：全体通知在前）
            assert notifs["unread_count"] == 1
            assert notifs["items"][0]["title"] == "全体通知"

    run(_case())