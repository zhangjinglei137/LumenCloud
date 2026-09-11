"""列表 _stats「已有 N 缺失 M」Emby 维度单测。

覆盖：
- 已有口径：Emby 入库 ∪ 本系统完成 去重合并
- Emby 故障（EmbyUnavailable）→ available 回退 done，接口 200
- missing 不为负（Emby 已有集数 > TMDB 全集数）
- 仅对 tv + 有缺失的影视触发 Emby 查询（total==done 时不调用）
"""
import itertools
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_list_emby_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

from datetime import date, datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.main import app  # noqa: E402

_TMDB_ID = itertools.count(2000)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import InviteCode, Notification, User, WatchRequest
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        for model in (Notification, InviteCode, WatchRequest):
            await session.execute(delete(model))
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None
    return password


def _login(client: TestClient) -> dict:
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return _auth(r.json()["access_token"])


async def _seed_tv_with_done(total_episodes: int):
    """tv + tmdb_id：tmdb_cache 全集数 total_episodes，本系统已完成 S01E01（1 集）。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media, TmdbCache

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tmid = next(_TMDB_ID)
    async with async_session() as session:
        media = Media(title="测试剧", media_type="tv", tmdb_id=tmid, status="tracking")
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        # 适配说明：TmdbCache 实际模型无 payload 字段，且 title/media_type 为必填
        # （nullable=False），故按实际模型补 title、去 payload。
        session.add(TmdbCache(tmdb_id=str(tmid), title="测试剧", media_type="tv",
                              number_of_episodes=total_episodes))
        await session.commit()
        return {"media_id": media.id}


@pytest.fixture(autouse=True)
def _clear_ingested_cache():
    from app.services import emby as emby_mod

    emby_mod._INGESTED_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()


def test_stats_union_emby_and_done(monkeypatch):
    """tv：Emby 入库 3 集（S01E02/03/04，无本地记录）+ 本系统完成 1 集（S01E01）→
    available=4，missing = 20-4 = 16。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        "S01E02", "S01E03", "S01E04"}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 20)["media_id"]
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 4
        assert item["episode_stats"]["missing"] == 16


def test_stats_dedup_overlap(monkeypatch):
    """Emby 入库与本地完成同集（S01E01）→ 去重后 available=1 而非 2。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        "S01E01"}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 20)["media_id"]
        r = client.get("/api/media", headers=h)
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        assert item["episode_stats"]["missing"] == 19


def test_stats_emby_failure_degrades(monkeypatch):
    """Emby 故障（抛 EmbyUnavailable）→ available 回退 done（1），接口 200。"""
    from app.services import emby as emby_mod
    from app.services.emby import EmbyUnavailable

    async def boom(*_a, **_k):
        raise EmbyUnavailable("emby down")

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", boom)

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 20)["media_id"]
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        assert item["episode_stats"]["missing"] == 19


def test_stats_missing_not_negative(monkeypatch):
    """Emby 已有集数（25）> TMDB 全集数（20）→ missing=0 而非 -5。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        f"S01E{i:02d}" for i in range(1, 26)}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 20)["media_id"]
        r = client.get("/api/media", headers=h)
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["missing"] == 0


def test_stats_no_emby_query_when_no_missing(monkeypatch):
    """本系统已完成 == 全集数（无缺失）→ 不触发 Emby 查询。

    适配说明：app 引擎为模块级文件 SQLite（跨测试保留数据），本文件前序用例
    seed 的 total=20 剧集会触发查询。故本用例先清空 media 相关表，仅保留
    自己 seed 的 total==done 剧集，再断言 Emby mock 不被 await。
    """
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import (
        DownloadQueue, DownloadTask, EpisodeState, Media, TaskQueue, TmdbCache,
        TransferQueue,
    )
    from app.services import emby as emby_mod

    async def _reset_media():
        async with async_session() as session:
            for model in (DownloadQueue, DownloadTask, EpisodeState,
                          TaskQueue, TransferQueue, TmdbCache):
                await session.execute(delete(model))
            await session.execute(delete(Media))
            await session.commit()

    mock = AsyncMock(return_value=set())
    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", mock)

    with TestClient(app) as client:
        client.portal.call(_reset_media)
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 1)["media_id"]  # done=1, total=1
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        mock.assert_not_awaited()


def test_stats_missing_excludes_unaired(monkeypatch):
    """未开播集不计入缺失：aired_total 只计 air_date<=today 的集（无日期视为已开播），
    total 保持 TMDB 全集数。

    缓存 4 集：today / 3 天前 / 无日期（均视为已开播）+ 30 天后（未开播）。
    aired_total=3，本系统完成 1 集 → missing=3-1=2（而非 total 口径的 30-1=29）。
    """
    from app.routers import media as media_mod
    from app.services import emby as emby_mod

    today = date.today()

    async def fake_episode_info(tmdb_id):
        return [
            {"season": 1, "episode": 1, "name": "A", "air_date": today.isoformat()},
            {"season": 1, "episode": 2, "name": "B",
             "air_date": (today - timedelta(days=3)).isoformat()},
            {"season": 1, "episode": 3, "name": "C",
             "air_date": (today + timedelta(days=30)).isoformat()},
            {"season": 1, "episode": 4, "name": "D", "air_date": None},
        ]

    monkeypatch.setattr(media_mod.tmdb, "get_episode_info", fake_episode_info)
    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value=set()))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done, 30)["media_id"]
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["total"] == 30  # 全集数口径不变
        assert item["episode_stats"]["available"] == 1
        assert item["episode_stats"]["missing"] == 2  # 仅已开播 3 集中缺 2 集
