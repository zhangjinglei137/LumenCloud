"""线上反馈修复单测（Q1/Q2/Q8）。

- Q1: TMDB search_multi 归一化结果增加 year 字段（movie→release_date、tv→
      first_air_date 前 4 位；person/缺失/非法格式 → None）
- Q2: POST /api/media（MediaCreate）支持 poster_path，落库并经 _media_dto 回显
- Q8: /api/logs 支持 tmdb_id 过滤（多个 media 可同 tmdb_id），返回项含
      media_title 与 tmdb_id（join media 装配）；media_id 过滤保持兼容

tmdb 用 fake httpx（不连真实服务）；media/logs 用隔离的 in-memory SQLite
（Base.metadata.create_all，不触全局 app.database engine / TestClient）。
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.config import settings
from app.database import Base
from app.models import Media, TaskRun
from app.routers.logs import list_logs
from app.routers.media import MediaCreate, create_media
from app.services import config_store
from app.services.tmdb import search_multi


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Q1: TMDB 搜索结果 year 字段
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        return _FakeResp(self._payload)


def _search_multi(payload, monkeypatch):
    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})  # 隔离 DB 注入值，保持 env fallback 确定
    monkeypatch.setattr(
        "app.services.tmdb.httpx.AsyncClient",
        lambda timeout=None: _FakeClient(payload),
    )
    return run(search_multi("测试"))


def test_search_multi_year_movie_tv(monkeypatch):
    results = _search_multi(
        {
            "results": [
                {"id": 1, "title": "电影A", "media_type": "movie",
                 "release_date": "2023-05-12", "poster_path": "/p1.jpg"},
                {"id": 2, "name": "剧集B", "media_type": "tv",
                 "first_air_date": "2021-01-01"},
            ]
        },
        monkeypatch,
    )
    assert results[0]["year"] == "2023"
    assert results[1]["year"] == "2021"
    # 原有字段不破
    assert results[0]["title"] == "电影A" and results[0]["tmdb_id"] == 1
    assert results[1]["poster_path"] is None


def test_search_multi_year_edges(monkeypatch):
    """person / 缺失日期 / 非法格式 → year=None（不因异常中断单条）。"""
    results = _search_multi(
        {
            "results": [
                {"id": 3, "name": "演员C", "media_type": "person"},
                {"id": 4, "title": "电影D", "media_type": "movie", "release_date": "bad-date"},
                {"id": 5, "name": "剧集E", "media_type": "tv"},  # 缺 first_air_date
                {"id": 6, "title": "电影F", "media_type": "movie", "release_date": ""},
            ]
        },
        monkeypatch,
    )
    assert [r["year"] for r in results] == [None, None, None, None]


# ---------------------------------------------------------------------------
# Q2: MediaCreate.poster_path 落库 + DTO 回显
# ---------------------------------------------------------------------------

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


def test_media_create_persists_poster_path(_db_maker):
    async def _case():
        async with _db_maker() as session:
            dto = await create_media(
                payload=MediaCreate(
                    title="测试影视", tmdb_id=123, media_type="movie",
                    poster_path="/t/p/w500/abc123.jpg",
                ),
                admin=MagicMock(),
                session=session,
            )
            assert dto["poster_path"] == "/t/p/w500/abc123.jpg"
            assert dto["title"] == "测试影视"
            # 确实落库（刷新后重查）
            saved = await session.get(Media, dto["id"])
            assert saved.poster_path == "/t/p/w500/abc123.jpg"
        # 不带 poster_path 的创建仍兼容（None）
        async with _db_maker() as session:
            dto = await create_media(
                payload=MediaCreate(title="无海报影视", tmdb_id=456, media_type="tv"),
                admin=MagicMock(),
                session=session,
            )
            assert dto["poster_path"] is None

    run(_case())


# ---------------------------------------------------------------------------
# Q8: /api/logs — tmdb_id 过滤 + media_title/tmdb_id 返回
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_logs(_db_maker):
    """seed：m1(tmdb=42) 两条 task_run、m2(tmdb=43) 一条、无关联 media 一条。"""
    async def _seed():
        async with _db_maker() as session:
            m1 = Media(title="剧A", tmdb_id=42, media_type="tv", status="tracking")
            m2 = Media(title="片B", tmdb_id=43, media_type="movie", status="tracking")
            session.add_all([m1, m2])
            await session.flush()
            session.add_all([
                TaskRun(task_type="scan_media", media_id=m1.id, status="success",
                        message="A1", started_at=_now()),
                TaskRun(task_type="transfer", media_id=m1.id, status="error",
                        message="A2", started_at=_now()),
                TaskRun(task_type="scan_media", media_id=m2.id, status="success",
                        message="B1", started_at=_now()),
                TaskRun(task_type="scan_media", media_id=9999, status="error",
                        message="orphan", started_at=_now()),
            ])
            await session.commit()

    run(_seed())
    return {}


def _call_list_logs(session, **overrides):
    """直接调用 list_logs（绕过 FastAPI Query 依赖注入，显式传 Query 参数默认值）。"""
    kwargs = {
        "admin": MagicMock(),
        "session": session,
        "task_type": None,
        "status": None,
        "media_id": None,
        "tmdb_id": None,
        "limit": 50,
        "offset": 0,
    }
    kwargs.update(overrides)
    return list_logs(**kwargs)


def test_logs_tmdb_filter_and_title(_db_maker):
    _seed_logs(_db_maker)

    async def _case():
        async with _db_maker() as session:
            # tmdb_id=42 → m1 的两条，均带 media_title/tmdb_id
            rows = await _call_list_logs(session, tmdb_id=42)
            assert len(rows) == 2
            assert all(r["media_title"] == "剧A" for r in rows)
            assert all(r["tmdb_id"] == 42 for r in rows)
            # 返回项保留既有字段
            assert {r["task_type"] for r in rows} == {"scan_media", "transfer"}

            # 无过滤 → 全部 4 条；无关联 media 的行 media_title/tmdb_id 为 None（outer join）
            all_rows = await _call_list_logs(session)
            assert len(all_rows) == 4
            orphan = [r for r in all_rows if r["message"] == "orphan"][0]
            assert orphan["media_title"] is None and orphan["tmdb_id"] is None

            # media_id 过滤保持兼容
            by_mid = await _call_list_logs(session, media_id=all_rows[0]["media_id"])
            assert all(r["media_id"] == all_rows[0]["media_id"] for r in by_mid)

            # media_id + tmdb_id 同时传 → AND（m1 而非 m2）
            m1_id = [r for r in all_rows if r["message"] == "A1"][0]["media_id"]
            both = await _call_list_logs(session, media_id=m1_id, tmdb_id=42)
            assert len(both) >= 1 and all(r["tmdb_id"] == 42 for r in both)
            none_case = await _call_list_logs(session, media_id=m1_id, tmdb_id=43)
            assert none_case == []

    run(_case())