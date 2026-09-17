"""C7：站内通知分页（list_notifications limit/offset + total）与定期清理单测。

覆盖（对应 fix-audit-issues Task C7 / Design D11）：
- 分页：超过单页数量时接口返回当前页 + total；offset 翻页正确且两页不重叠。
- unread_count 不受分页影响（全量范围独立统计，分页只影响列表）。
- 清理：prune_history_job 按 created_at 保留期删除超期通知；保留期内不受影响；
  沿用 task_run / quark_capacity_log 同一 retention 口径（默认 30 天 / config 覆盖）。

测试方式：
- 分页：直接调 list_notifications 路由函数（绕过 Depends，参照 test_join_username.py）。
- 清理：隔离 in-memory SQLite（StaticPool 共享连接）+ monkeypatch cleanup.async_session
  （参照 test_prune_history.py），不触全局 app.database engine / TestClient。
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Notification, SystemConfig
from app.routers.notifications import list_notifications
from app.tasks import cleanup as cleanup_module
from app.tasks.cleanup import prune_history_job

ADMIN = types.SimpleNamespace(id=1, role="admin")


def run(coro):
    return asyncio.run(coro)


def _past(days: int) -> datetime:
    """naive UTC now 减去 days 天（与任务模块 _now / cutoff 同构）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


@pytest.fixture()
def _db_maker():
    """隔离 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构。"""
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


def _use_test_db(monkeypatch, maker):
    """prune_history_job 使用测试库的连接工厂。"""
    monkeypatch.setattr(cleanup_module, "async_session", maker)


async def _seed_notifications(s, n: int, *, is_read: bool = False):
    """seed n 条收件人为当前用户（id=1）的通知（created_at 走 DB 默认值）。"""
    s.add_all([
        Notification(recipient=1, event_type="approval_pending",
                     title=f"通知{i}", body="b", is_read=is_read)
        for i in range(n)
    ])
    await s.commit()


# ---------------------------------------------------------------------------
# 分页：limit 截断当前页 + total 为全量
# ---------------------------------------------------------------------------

def test_list_notifications_pagination_limits_items_and_total(_db_maker):
    """60 条通知、limit=50 → 返回 50 条 + total=60（不得再一次性返回全部）。"""

    async def _seed():
        async with _db_maker() as s:
            await _seed_notifications(s, 60)

    run(_seed())

    async def _case():
        async with _db_maker() as s:
            notifs = await list_notifications(user=ADMIN, session=s, limit=50, offset=0)
            assert len(notifs["items"]) == 50
            assert notifs["total"] == 60
            assert notifs["unread_count"] == 60

    run(_case())


def test_list_notifications_pagination_offset(_db_maker):
    """limit=50 + offset=50 → 返回剩余 10 条，total 仍为 60；两页 id 不重叠。"""

    async def _seed():
        async with _db_maker() as s:
            await _seed_notifications(s, 60)

    run(_seed())

    async def _case():
        async with _db_maker() as s:
            page1 = await list_notifications(user=ADMIN, session=s, limit=50, offset=0)
            page2 = await list_notifications(user=ADMIN, session=s, limit=50, offset=50)
            assert len(page1["items"]) == 50
            assert len(page2["items"]) == 10
            assert page1["total"] == 60 and page2["total"] == 60
            ids1 = {it["id"] for it in page1["items"]}
            ids2 = {it["id"] for it in page2["items"]}
            assert ids1.isdisjoint(ids2)

    run(_case())


# ---------------------------------------------------------------------------
# unread_count 不受分页影响（全量范围独立统计）
# ---------------------------------------------------------------------------

def test_unread_count_full_scope_not_affected_by_pagination(_db_maker):
    """30 未读 + 10 已读，limit=10 → items 只 10 条，unread_count 仍为 30。"""

    async def _seed():
        async with _db_maker() as s:
            await _seed_notifications(s, 30, is_read=False)
            await _seed_notifications(s, 10, is_read=True)

    run(_seed())

    async def _case():
        async with _db_maker() as s:
            notifs = await list_notifications(user=ADMIN, session=s, limit=10, offset=0)
            assert len(notifs["items"]) == 10
            assert notifs["total"] == 40
            assert notifs["unread_count"] == 30

    run(_case())


# ---------------------------------------------------------------------------
# 清理：超期通知按 created_at 保留期删除（默认 30 天）
# ---------------------------------------------------------------------------

def test_prune_history_deletes_expired_notifications(_db_maker, monkeypatch):
    """40 天前的通知被删除；5 天前（保留期内）不受影响。"""
    _use_test_db(monkeypatch, _db_maker)

    async def _seed():
        async with _db_maker() as s:
            s.add_all([
                Notification(recipient=1, event_type="approval_pending",
                             title="old40", body="b", created_at=_past(40)),
                Notification(recipient=1, event_type="download_complete",
                             title="new5", body="b", created_at=_past(5)),
            ])
            await s.commit()

    run(_seed())
    run(prune_history_job())

    async def _verify():
        async with _db_maker() as s:
            titles = [
                n.title for n in
                (await s.execute(select(Notification))).scalars().all()
            ]
            assert titles == ["new5"]

    run(_verify())


# ---------------------------------------------------------------------------
# 清理：system_config 保留天数覆盖对通知同样生效
# ---------------------------------------------------------------------------

def test_prune_history_notifications_retention_override(_db_maker, monkeypatch):
    """config 保留 7 天 → 8 天前通知被删、5 天前保留（默认 30 天下两者都保留）。"""
    _use_test_db(monkeypatch, _db_maker)

    async def _seed():
        async with _db_maker() as s:
            s.add_all([
                Notification(recipient=1, event_type="approval_pending",
                             title="old8", body="b", created_at=_past(8)),
                Notification(recipient=1, event_type="download_complete",
                             title="new5", body="b", created_at=_past(5)),
                SystemConfig(key="task_run_retention_days", value="7"),
            ])
            await s.commit()

    run(_seed())
    run(prune_history_job())

    async def _verify():
        async with _db_maker() as s:
            titles = [
                n.title for n in
                (await s.execute(select(Notification))).scalars().all()
            ]
            assert titles == ["new5"]

    run(_verify())
