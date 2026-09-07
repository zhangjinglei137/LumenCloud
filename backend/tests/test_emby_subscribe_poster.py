"""Emby 订阅海报回填单测：POST /media 创建时从 TMDB 幂等回填 poster_path。

背景（需求）：从 Emby 影视库「加入订阅」的影视，在影视库中不显示海报图。
根因：EmbyLibraryView.subscribe 不传 poster_path（Emby 海报是完整 URL，与
poster_path 的 TMDB 相对路径语义不同，故订阅时不传）→ 后端落库 NULL → 巡检
不回填 → 影视库卡片/表格只显示标题首字占位、无图。

修复：create_media 创建时幂等回填——有传入 poster_path 优先（TMDB 手动添加 /
访客「想看」审批透传不受影响）；为空且 tmdb_id 非空则从 get_by_tmdb_id 元数据
取 poster_path 落库；拿不到保持 None（fail-open，TMDB 无海报的影视本来无图）。

外部依赖全部 monkeypatch（app.services.tmdb.get_by_tmdb_id），不连真实服务。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media
from app.routers.media import MediaCreate, create_media


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    """独立 in-memory SQLite（StaticPool 共享连接）→ 返回 sessionmaker。"""
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


def test_create_media_emby_subscribe_backfills_poster(db, monkeypatch):
    """Emby 订阅场景：不传 poster_path、有 tmdb_id → 海报由 TMDB 元数据回填。"""
    monkeypatch.setattr(
        "app.services.tmdb.get_by_tmdb_id",
        AsyncMock(
            return_value={
                "tmdb_id": "42",
                "title": "Emby订阅影视",
                "media_type": "movie",
                "poster_path": "/abc123.jpg",
                "year": "2020",
                "status": "Released",
                "tv_status": "Released",
            }
        ),
    )

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(title="Emby订阅影视", tmdb_id=42, media_type="movie"),
                admin=MagicMock(),
                session=s,
            )
            assert dto["poster_path"] == "/abc123.jpg"  # 落库即回填
        async with db() as s:
            row = (await s.execute(select(Media))).scalars().first()
            assert row is not None and row.poster_path == "/abc123.jpg"

    run(_case())


def test_create_media_keeps_explicit_poster(db, monkeypatch):
    """传入 poster_path 优先：手动添加/TMDB 搜索传了海报，不被 TMDB 回填覆盖。"""
    monkeypatch.setattr(
        "app.services.tmdb.get_by_tmdb_id",
        AsyncMock(return_value={"poster_path": "/tmdb_other.jpg", "status": None}),
    )

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(
                    title="手动添加",
                    tmdb_id=7,
                    media_type="movie",
                    poster_path="/manual.jpg",
                ),
                admin=MagicMock(),
                session=s,
            )
            assert dto["poster_path"] == "/manual.jpg"

    run(_case())


def test_create_media_tmdb_no_poster_stays_none(db, monkeypatch):
    """TMDB 元数据无海报 → 落库 None（fail-open，不阻断、不抛错）。"""
    monkeypatch.setattr(
        "app.services.tmdb.get_by_tmdb_id",
        AsyncMock(return_value={"poster_path": None, "status": None}),
    )

    async def _case():
        async with db() as s:
            dto = await create_media(
                payload=MediaCreate(title="无海报影视", tmdb_id=9, media_type="movie"),
                admin=MagicMock(),
                session=s,
            )
            assert dto["poster_path"] is None

    run(_case())
