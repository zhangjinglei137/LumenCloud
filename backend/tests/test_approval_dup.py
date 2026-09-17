"""Q2①（P1）：审批 / 手动添加影视 tmdb_id 去重（应用层 409 + DB UNIQUE 兜底）。

覆盖三个写入入口的应用层查重：
- POST /api/media                    （create_media）
- POST /api/approvals                （create_approval）
- POST /api/approvals/{id}/approve   （approve_approval）

数据库用隔离的 in-memory SQLite（Base.metadata.create_all，含 Media.tmdb_id
UNIQUE 约束），直接调用路由函数（admin/user 注入 MagicMock，session 注入测试引擎）。
approve_approval 的事务外副作用（notifier / 批准后自动巡检触发）monkeypatch：
E-1（P1）起 approve 只做 fire-and-forget 触发——notifier.notify 用 AsyncMock、
trigger_scan_background 用同步记录器；approve 内是调用时才 import，patch 模块属性
`app.tasks.scan.trigger_scan_background` 即可生效。
"""
import asyncio
import types
from unittest.mock import AsyncMock, MagicMock  # MagicMock 仅用于 admin（不落库）

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media, WatchRequest
from app.routers.approvals import WatchRequestCreate, approve_approval, create_approval
from app.routers.media import MediaCreate, create_media

# 与三个入口共用的 409 文案（改动时须同步）
DUP_DETAIL = "该影视已在影视库，无需重复提交"

# user.id / admin.id 会落库（requested_by / reviewed_by），须是真实 int——
# MagicMock().id 是嵌套 MagicMock，SQLite 绑定参数会报 ProgrammingError
AKA = types.SimpleNamespace(id=1)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _db_maker():
    """隔离的 in-memory SQLite（StaticPool 共享连接），create_all 最新模型结构（含 tmdb_id UNIQUE）。"""
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


def _seed_media(_db_maker, *, tmdb_id: int):
    """预置一条 Media(tmdb_id)。"""

    async def _seed():
        async with _db_maker() as session:
            session.add(
                Media(
                    title=f"已有影视{tmdb_id}", tmdb_id=tmdb_id,
                    media_type="movie", status="tracking",
                )
            )
            await session.commit()

    run(_seed())


# ---------------------------------------------------------------------------
# 1) create_media：已有 Media(tmdb_id=42) 时再建 42 → 409；43 / None 正常
# ---------------------------------------------------------------------------

def test_create_media_dup_tmdb_409(_db_maker):
    _seed_media(_db_maker, tmdb_id=42)

    async def _case():
        # 重复 tmdb_id → 409（写入前拦截，不落库）
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await create_media(
                    payload=MediaCreate(title="重复影视", tmdb_id=42, media_type="movie"),
                    admin=MagicMock(),
                    session=session,
                )
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        # 不同 tmdb_id → 正常创建
        async with _db_maker() as session:
            dto = await create_media(
                payload=MediaCreate(title="新影视", tmdb_id=43, media_type="movie"),
                admin=MagicMock(),
                session=session,
            )
            assert dto["tmdb_id"] == 43
        # tmdb_id=None → 不做查重，正常创建（UNIQUE 允许多个 NULL）
        async with _db_maker() as session:
            dto = await create_media(
                payload=MediaCreate(title="无tmdb影视A", tmdb_id=None, media_type="tv"),
                admin=MagicMock(),
                session=session,
            )
            assert dto["tmdb_id"] is None
        # 同 DB 再建一条 tmdb_id=None → 仍正常（NULL 不触发 UNIQUE）
        async with _db_maker() as session:
            dto = await create_media(
                payload=MediaCreate(title="无tmdb影视B", tmdb_id=None, media_type="tv"),
                admin=MagicMock(),
                session=session,
            )
            assert dto["tmdb_id"] is None

    run(_case())


# ---------------------------------------------------------------------------
# 2) create_approval：已有 Media(tmdb_id=42) 时提交 42 → 409；43 → 正常 pending
# ---------------------------------------------------------------------------

def test_create_approval_dup_tmdb_409(_db_maker, monkeypatch):
    _seed_media(_db_maker, tmdb_id=42)
    # 通知走 mock：InAppNotifier 会用全局 async_session 写库，隔离掉
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())

    async def _case():
        # 提交已存在于影视库的 tmdb_id → 409（写入前拦截）
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await create_approval(
                    payload=WatchRequestCreate(title="重复想看", tmdb_id=42, media_type="movie"),
                    user=AKA,
                    session=session,
                )
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        # 不同 tmdb_id → 正常写入 WatchRequest(pending)
        async with _db_maker() as session:
            res = await create_approval(
                payload=WatchRequestCreate(title="新想看", tmdb_id=43, media_type="tv"),
                user=AKA,
                session=session,
            )
            wr = await session.get(WatchRequest, res["id"])
            assert wr.tmdb_id == 43 and wr.status == "pending"

    run(_case())


# ---------------------------------------------------------------------------
# 3) approve_approval：wr.tmdb_id=42 且 Media 已存在 → 409 且保持 pending；
#    wr.tmdb_id 不重复 → 正常批准 + 创建 Media
# ---------------------------------------------------------------------------

def test_approve_dup_tmdb_409_keeps_pending(_db_maker, monkeypatch):
    _seed_media(_db_maker, tmdb_id=42)
    # 事务外副作用全部 mock：站内通知 + 批准后自动巡检触发。E-1 起 approve 只做
    # fire-and-forget 触发——同步记录器（记录 media_id）替代 trigger_scan_background，
    # 不跑真实巡检（patch 模块属性，延迟 import 即生效）
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id, **kwargs: trigger_calls.append(media_id),
    )

    def _seed_wr(tmdb_id: int) -> int:
        async def _seed():
            async with _db_maker() as session:
                wr = WatchRequest(
                    requested_by=1, title=f"想看{tmdb_id}", tmdb_id=tmdb_id,
                    media_type="movie", status="pending",
                )
                session.add(wr)
                await session.commit()
                return wr.id

        return run(_seed())

    dup_wr_id = _seed_wr(tmdb_id=42)  # 与已有 Media 撞车
    ok_wr_id = _seed_wr(tmdb_id=43)   # 不重复

    # 撞车 → 409 且 WatchRequest 未被消费（仍 pending，管理员可另行 reject）
    async def _case_dup():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await approve_approval(dup_wr_id, admin=AKA, session=session)
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        # 独立会话复查（避免同一会话身份映射读到旧状态）
        async with _db_maker() as session:
            assert (await session.get(WatchRequest, dup_wr_id)).status == "pending"
        assert trigger_calls == []  # 409 在事务副作用之前抛出，绝不触发巡检

    run(_case_dup())

    # 不重复 → 正常批准：Media(tmdb_id=43) 落地 + wr 变 approved
    async def _case_ok():
        async with _db_maker() as session:
            res = await approve_approval(ok_wr_id, admin=AKA, session=session)
            assert res["ok"] is True and res["media_id"] is not None
        async with _db_maker() as session:
            media = await session.get(Media, res["media_id"])
            assert media is not None and media.tmdb_id == 43 and media.status == "tracking"
            assert (await session.get(WatchRequest, ok_wr_id)).status == "approved"
        # E-1：批准后只做 fire-and-forget 触发（同步记录器收到新 media 的 id）
        assert trigger_calls == [res["media_id"]]

    run(_case_ok())


# ---------------------------------------------------------------------------
# 4) approve_approval tmdb_id=None 时以 title 大小写不敏感精确查重兜底（C11/
#    审查 C15）：非 TMDB 条目同名不再重复入库；tmdb_id 非空路径不受影响
# ---------------------------------------------------------------------------

def test_approve_dup_title_when_tmdb_none(_db_maker, monkeypatch):
    """tmdb_id=None 的审批：Media 已存在同 title（大小写不同）→ 409 且保持 pending。

    当前实现 tmdb_id is None 时跳过查重 → 放行（RED）。要求：以 title 大小写
    不敏感兜底查重，命中 → 409，wr 保持 pending，不触发批准后巡检。
    """
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id, **kwargs: trigger_calls.append(media_id),
    )

    async def _seed_media():
        async with _db_maker() as session:
            # 已存在 Media(title="同名影视", tmdb_id=None)——大小写与 wr.title 不同
            session.add(
                Media(
                    title="同名影视", tmdb_id=None,
                    media_type="movie", status="tracking",
                )
            )
            await session.commit()

    run(_seed_media())

    async def _seed_wr() -> int:
        async with _db_maker() as session:
            wr = WatchRequest(
                requested_by=1, title="同名影视", tmdb_id=None,
                media_type="movie", status="pending",
            )
            session.add(wr)
            await session.commit()
            return wr.id

    wr_id = run(_seed_wr())

    async def _case_dup():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await approve_approval(wr_id, admin=AKA, session=session)
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        # 独立会话复查：wr 未被消费（仍 pending）
        async with _db_maker() as session:
            assert (await session.get(WatchRequest, wr_id)).status == "pending"
        assert trigger_calls == []

    run(_case_dup())


def test_approve_title_dup_is_case_insensitive(_db_maker, monkeypatch):
    """title 兜底查重大小写不敏感：Media(title="The Movie") vs wr.title="the movie" → 409。"""
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id, **kwargs: trigger_calls.append(media_id),
    )

    async def _seed_media():
        async with _db_maker() as session:
            session.add(
                Media(
                    title="The Movie", tmdb_id=None,
                    media_type="movie", status="tracking",
                )
            )
            await session.commit()

    run(_seed_media())

    async def _seed_wr() -> int:
        async with _db_maker() as session:
            wr = WatchRequest(
                requested_by=1, title="the movie", tmdb_id=None,
                media_type="tv", status="pending",
            )
            session.add(wr)
            await session.commit()
            return wr.id

    wr_id = run(_seed_wr())

    async def _case():
        async with _db_maker() as session:
            with pytest.raises(HTTPException) as ei:
                await approve_approval(wr_id, admin=AKA, session=session)
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        assert trigger_calls == []

    run(_case())


def test_approve_title_no_dup_when_tmdb_none_ok(_db_maker, monkeypatch):
    """tmdb_id=None 且 title 不重复 → 正常批准（title 兜底不误伤新条目）。"""
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id, **kwargs: trigger_calls.append(media_id),
    )

    async def _seed_wr() -> int:
        async with _db_maker() as session:
            wr = WatchRequest(
                requested_by=1, title="全新影视", tmdb_id=None,
                media_type="movie", status="pending",
            )
            session.add(wr)
            await session.commit()
            return wr.id

    wr_id = run(_seed_wr())

    async def _case():
        async with _db_maker() as session:
            res = await approve_approval(wr_id, admin=AKA, session=session)
            assert res["ok"] is True and res["media_id"] is not None
        async with _db_maker() as session:
            media = await session.get(Media, res["media_id"])
            assert media is not None and media.title == "全新影视" and media.tmdb_id is None
            assert (await session.get(WatchRequest, wr_id)).status == "approved"
        assert trigger_calls == [res["media_id"]]

    run(_case())


# ---------------------------------------------------------------------------
# 5) approve_approval 并发竞争窗口：查重通过（返回「无重复」）后另一路已建
#    Media → 插入撞 UNIQUE → 捕获 IntegrityError 返回 409（而非裸 500），
#    且事务回滚后 wr 保持 pending（可再次尝试）
# ---------------------------------------------------------------------------

def test_approve_dup_race_window_409(_db_maker, monkeypatch):
    """并发审批同 tmdb_id：模拟「查重与插入之间的竞争窗口」。

    两个 admin 并发批准引用同一 tmdb_id 的两个不同 wr 时，双方查重都在对方
    commit 前通过（monkeypatch 让查重恒返回「无重复」），后提交者在 flush 时
    撞 Media.tmdb_id UNIQUE → IntegrityError。要求：捕获后返回 409（替代裸
    500），事务回滚使 wr 保持 pending（可再次尝试），且不触发批准后巡检。
    """
    _seed_media(_db_maker, tmdb_id=42)  # 实际 DB 已存在同 tmdb_id 的 Media
    monkeypatch.setattr("app.routers.approvals.notifier.notify", AsyncMock())
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id, **kwargs: trigger_calls.append(media_id),
    )

    async def _seed_wr(tmdb_id: int) -> int:
        async with _db_maker() as session:
            wr = WatchRequest(
                requested_by=1, title=f"想看{tmdb_id}", tmdb_id=tmdb_id,
                media_type="movie", status="pending",
            )
            session.add(wr)
            await session.commit()
            return wr.id

    wr_id = run(_seed_wr(tmdb_id=42))

    async def _case():
        async with _db_maker() as session:
            # 模拟竞争窗口：查重恒返回「无重复」（近似并发下查重先于对方 commit
            # 通过），但 DB 实际已有 Media(tmdb_id=42) → flush 撞 UNIQUE
            monkeypatch.setattr(session, "scalar", AsyncMock(return_value=None))
            with pytest.raises(HTTPException) as ei:
                await approve_approval(wr_id, admin=AKA, session=session)
            assert ei.value.status_code == 409
            assert ei.value.detail == DUP_DETAIL
        # 独立会话复查：事务已回滚，pending→approved 更新未落盘 → 仍 pending
        async with _db_maker() as session:
            assert (await session.get(WatchRequest, wr_id)).status == "pending"
        # 409 在事务副作用之前抛出，绝不触发巡检
        assert trigger_calls == []

    run(_case())
