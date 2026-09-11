"""详情接口 active_tasks 字段单测（「当前进行中任务」数据源）。

覆盖：
- download_queue 进行中 + task_queue 进行中合并，source/status 透传
- 终态剔除（done/failed/skipped 不出现）
- 已入库剔除（code 命中 in_emby_codes）
- 未到首播日剔除（air_date 未来）；air_date 未知保留
- 排序 (season, episode) 升序
- movie 不返回该字段
"""
import itertools
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_active_tasks_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.main import app  # noqa: E402

_TMDB_ID = itertools.count(3000)


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


async def _seed_tv_mixed_queue():
    """tv：dq 行 S01E01 downloading（进行中）、S01E02 done（终态）、
    tq 行 S01E03 probing（进行中）、S01E04 ready（进行中）。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="测试剧", media_type="tv", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.flush()
        for ep, status in (("S01E01", "downloading"), ("S01E02", "done")):
            session.add(DownloadQueue(
                media_id=media.id, episode=ep, file_name=f"{ep}.mkv",
                file_size=1024**3, share_code="ScAa1111", status=status,
                enqueued_at=now, updated_at=now,
            ))
        for ep, status in (("S01E03", "probing"), ("S01E04", "ready")):
            session.add(TaskQueue(
                media_id=media.id, episode=ep, status=status,
                created_at=now, updated_at=now,
            ))
        await session.commit()
        return {"media_id": media.id}


async def _seed_movie():
    from app.database import async_session
    from app.models import Media

    async with async_session() as session:
        media = Media(title="电影", media_type="movie", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.commit()
        return {"media_id": media.id}


@pytest.fixture(autouse=True)
def _clear_caches():
    from app.services import emby as emby_mod
    from app.services import tmdb as tmdb_mod

    emby_mod._INGESTED_CACHE.clear()
    tmdb_mod._SEASON_AIR_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()
    tmdb_mod._SEASON_AIR_CACHE.clear()


def _mock_emby(monkeypatch, emby_id="e1", episodes=None):
    """mock Emby：find_emby_id 命中 e1；已入库集 episodes（默认空=全部未入库）。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=emby_id))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=episodes or []))


def test_active_tasks_merge_and_source(monkeypatch):
    """dq + tq 进行中合并；终态 done 剔除；source 区分 dq/task。"""
    _mock_emby(monkeypatch)
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        tasks = r.json()["active_tasks"]
        by_ep = {t["episode"]: t for t in tasks}
        assert "S01E02" not in by_ep  # done 终态剔除
        assert by_ep["S01E01"]["source"] == "dq"
        assert by_ep["S01E01"]["status"] == "downloading"
        assert by_ep["S01E03"]["source"] == "task"
        assert by_ep["S01E03"]["status"] == "probing"
        assert by_ep["S01E04"]["source"] == "task"
        assert by_ep["S01E04"]["status"] == "ready"


def test_active_tasks_excludes_ingested(monkeypatch):
    """已入库集（code 命中 Emby）剔除：S01E01 在 Emby → 不出现；S01E03 不在 → 出现。"""
    _mock_emby(monkeypatch, episodes=[{"code": "S01E01"}])
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        tasks = {t["episode"] for t in r.json()["active_tasks"]}
        assert "S01E01" not in tasks
        assert "S01E03" in tasks


def test_active_tasks_excludes_not_aired(monkeypatch):
    """未到首播日剔除：S01E03 air_date 未来 → 剔除；air_date 缺失 → 保留。"""
    from app.services import tmdb as tmdb_mod
    from app.services.tmdb import get_tv_all_episodes

    _mock_emby(monkeypatch)
    # tmdb_episodes 全集轴：S01E03 air_date 未来；S01E04 无 air_date
    monkeypatch.setattr(tmdb_mod, "get_episode_info", AsyncMock(return_value=[
        {"season": 1, "episode": 3, "air_date": "2099-01-01", "name": "E03"},
        {"season": 1, "episode": 4, "name": "E04"},
    ]))
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", AsyncMock(
        side_effect=get_tv_all_episodes))  # 兜底不被调用

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        tasks = {t["episode"] for t in r.json()["active_tasks"]}
        assert "S01E03" not in tasks  # 未到首播日
        assert "S01E04" in tasks  # air_date 未知保留


def test_active_tasks_movie_absent(monkeypatch):
    """movie 详情不返回 active_tasks 字段。"""
    _mock_emby(monkeypatch)
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_movie)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert "active_tasks" not in r.json()