"""scan「防重基线缺失」软处理单测（scan_baseline_required 开关）。

背景（用户痛点）：想下载本地没有的剧集时 Emby 未收录是常态，旧逻辑 tv 未收录
→ 基线不可用 → 本轮硬跳过。改造后默认（scan_baseline_required=False）按全量模式
照常搜索入队（只转搜索到的具体文件），仅当部署显式开启强防重才回归旧行为。

覆盖：
- _emby_missing_codes：tv 未收录 + 默认开关 → [None]（全量模式等价表达）；
  tv 未收录 + 开关开启 → None（基线不可用，主流程跳过）；movie 行为保持不变；
  tv 已收录 → 缺失集列表
- _scan_one 端到端：tv 未收录默认放行 → 全量搜索入队（EpisodeState queued）；
  开关开启 → skipped（文案区分「防重基线强制」）
"""
import asyncio
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import EpisodeState, Media, TaskRun, TransferQueue


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def _mock_baseline_config(monkeypatch, value):
    """让 config_store.get('scan_baseline_required') 返回指定值，其余键走原逻辑。"""
    from app.services import config_store as cs

    original = cs.get

    def fake_get(key, default=None):
        if key == "scan_baseline_required":
            return value
        return original(key, default)

    monkeypatch.setattr(cs, "get", fake_get)


def _tv_media(emby_id):
    """mock emby.find_emby_id 返回 emby_id（None = 未收录）。"""
    return types.SimpleNamespace(tmdb_id=42, title="测试剧", media_type="tv", id=1)


# ---------------------------------------------------------------------------
# _emby_missing_codes 分流
# ---------------------------------------------------------------------------

def test_emby_missing_codes_tv_missing_default_returns_full_mode(monkeypatch):
    """tv 未收录 + 默认开关（False）→ [None]（全量模式，不再硬跳过）。"""
    from app.tasks import scan as scan_mod

    _mock_baseline_config(monkeypatch, None)  # 未配置 → settings 默认 False
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value=None))

    missing = run(scan_mod._emby_missing_codes(_tv_media(None)))

    assert missing == [None]  # 与 movie 全量模式同一表达


def test_emby_missing_codes_tv_missing_required_returns_none(monkeypatch):
    """tv 未收录 + 开关开启（True）→ None（基线不可用，主流程跳过，旧行为）。"""
    from app.tasks import scan as scan_mod

    _mock_baseline_config(monkeypatch, "true")
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value=None))

    missing = run(scan_mod._emby_missing_codes(_tv_media(None)))

    assert missing is None


def test_emby_missing_codes_tv_present_returns_missing(monkeypatch):
    """tv 已收录 → 缺失集 code 列表（None code 过滤，行为不变）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="emby_1"))
    monkeypatch.setattr(
        scan_mod.emby, "get_missing_episodes",
        AsyncMock(return_value=[{"code": "S01E01"}, {"code": None}, {"code": "S01E02"}]),
    )

    missing = run(scan_mod._emby_missing_codes(_tv_media("emby_1")))

    assert missing == ["S01E01", "S01E02"]


def test_emby_missing_codes_movie_behavior_unchanged(monkeypatch):
    """movie 行为保持不变：整部缺失 [None] / 已在库 []。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value=None))
    media = types.SimpleNamespace(tmdb_id=42, title="测试电影", media_type="movie", id=1)
    assert run(scan_mod._emby_missing_codes(media)) == [None]

    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="emby_1"))
    assert run(scan_mod._emby_missing_codes(media)) == []


# ---------------------------------------------------------------------------
# _scan_one 端到端：tv 未收录（Emby 无基线）
# ---------------------------------------------------------------------------

async def _seed_media(db, *, media_type="tv"):
    async with db() as s:
        media = Media(title="测试剧", media_type=media_type, tmdb_id=42, status="tracking")
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_env(monkeypatch, db, *, find_emby_id=None, search=None, share_list=None):
    """scan 外部依赖全 mock：emby 未收录 + cloudSaver 搜索/分享返回固定结果。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value=find_emby_id))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "search",
        AsyncMock(return_value=search if search is not None else []),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                               "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value=share_list if share_list is not None else {"list": []}),
    )
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


def test_scan_one_tv_missing_default_full_mode_enqueues(db, monkeypatch):
    """tv 未收录 + 默认开关 → 全量搜索入队：搜索到的具体文件视为缺失集。"""
    search = [{
        "title": "测试剧 S01",
        "cloud_links": [{"cloud_type": "quark", "link": "https://pan.quark.cn/s/abc123"}],
    }]
    share_list = {"list": [{
        "fileName": "S01E01.mkv", "fileId": "f1", "fileIdToken": "ft1",
        "isFolder": False, "size": 1024,
    }]}
    _mock_baseline_config(monkeypatch, None)  # 默认 False：软处理放行
    scan_mod = _patch_scan_env(monkeypatch, db, find_emby_id=None, search=search,
                               share_list=share_list)

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))

    # task_run：success + 全量入队 1 条
    async def _read_run():
        async with db() as s:
            tr = (await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
            )).scalars().first()
            es = (await s.execute(
                select(EpisodeState).where(EpisodeState.media_id == mid)
            )).scalars().first()
            tq = (await s.execute(
                select(TransferQueue).where(TransferQueue.media_id == mid)
            )).scalars().first()
            return tr, es, tq
    tr, es, tq = run(_read_run())

    assert tr.status == "success"
    assert "入队1" in tr.message
    # 全量模式：episode=文件名，具体文件被视为缺失集入队
    assert es.episode == "S01E01.mkv"
    assert es.state == "queued"
    assert tq.status == "pending"
    assert rid == tr.id


def test_scan_one_tv_missing_required_skips(db, monkeypatch):
    """tv 未收录 + 开关开启 → 本轮跳过（旧行为），文案区分「防重基线强制」。"""
    _mock_baseline_config(monkeypatch, "true")
    scan_mod = _patch_scan_env(monkeypatch, db, find_emby_id=None)

    mid = run(_seed_media(db))
    rid = run(scan_mod._scan_one(mid))

    async def _read_run():
        async with db() as s:
            tr = (await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
            )).scalars().first()
            es = (await s.execute(
                select(EpisodeState).where(EpisodeState.media_id == mid)
            )).scalars().first()
            return tr, es
    tr, es = run(_read_run())

    assert tr.status == "skipped"
    assert "防重基线强制" in tr.message
    assert "scan_baseline_required=True" in tr.message
    assert es is None  # 未入队
    assert rid == tr.id
