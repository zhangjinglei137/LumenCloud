"""影视详情页集数状态 tag 数据字段单测（in_emby / air_date）。

背景（前端需求）：详情页集数列表每集显示 4 色 tag——绿=已在 Emby（in_emby=True）、
灰=未开播（air_date 在未来，由前端比较）、黄=已开播、红=异常。后端 GET /api/media/{id}
的 episode_state 每行新增两字段：
- in_emby: bool  该集是否已在 Emby 库
- air_date: str | null  该集 TMDB 首播日期 "YYYY-MM-DD"
非 tv、无 tmdb_id、Emby/TMDB 故障一律降级（in_emby=False / air_date=None），
绝不阻断详情接口。

覆盖：
- tv + tmdb_id 集成：Emby 库内集 in_emby=True；非库内集 False；air_date 按
  (season, episode_number) 对上；旧 episode_state 行（旧 DTO）同样带两字段
- Emby 故障（find_emby_id 抛异常）→ 接口仍 200，in_emby 全 False，air_date 不受影响
- movie → 不触发查询逻辑，in_emby/air_date 恒 False/None，接口 200
- episode 非标准格式（如文件名）→ in_emby=False、air_date=None，接口 200
- tmdb.get_tv_season_air_dates 单测：回源正常 / 非 200 降级 / 进程内 TTL 缓存命中
"""
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_media_tags_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 隔离外部服务：避免测试发起真实外部网络调用
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["TMDB_HTTP_PROXY"] = ""
os.environ["CLOUIDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

from datetime import datetime, timezone  # noqa: E402
import itertools  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.main import app  # noqa: E402
from app.services import tmdb as tmdb_mod  # noqa: E402

# app 引擎为模块级文件 SQLite（跨测试保留数据），media.tmdb_id UNIQUE——
# seed 用自增 id 避免跨测试撞唯一约束
_TMDB_ID = itertools.count(1000)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。"""
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
    """admin 登录，返回鉴权头。"""
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return _auth(r.json()["access_token"])


async def _seed_tv_dq_es():
    """tv + tmdb_id：download_queue S01E01 + 旧 episode_state S01E02（兼容合并）。"""
    from app.database import async_session
    from app.models import DownloadQueue, EpisodeState, Media

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="少帅", media_type="tv", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        session.add(
            EpisodeState(
                media_id=media.id, episode="S01E02", state="done",
                file_name="f02.mkv", file_size=1024**3, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _seed_movie():
    """movie + tmdb_id：movie 不触发 in_emby/air_date 查询逻辑。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="大话西游", media_type="movie", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _seed_non_standard_episode():
    """tv：episode 为非标准格式（全量模式文件名），无 season/episode_number。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="少帅", media_type="tv", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="少帅将我宠上天.mp4", file_name="x.mp4",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


@pytest.fixture(autouse=True)
def _clear_season_air_cache():
    """清空 tmdb 模块级 season air_date TTL 缓存（跨测试隔离）。"""
    tmdb_mod._SEASON_AIR_CACHE.clear()
    yield
    tmdb_mod._SEASON_AIR_CACHE.clear()


# ---------------------------------------------------------------------------
# 1) tv + tmdb_id 集成：in_emby / air_date 正确装配
# ---------------------------------------------------------------------------

def test_detail_tv_in_emby_and_air_date(monkeypatch):
    """tv + tmdb_id：S01E01 在 Emby 库 → in_emby=True；S01E02 不在 → False；
    air_date 按 (season, episode_number) 对上（新表 dq 行与旧表 es 行均有字段）。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[
        {"code": "S01E01"}, {"code": "S01E03"},
    ]))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", AsyncMock(return_value={
        1: "2026-01-01", 2: "2026-01-02",
    }))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_dq_es)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}

        e01 = by_ep["S01E01"]  # download_queue 行（新 DTO）
        assert e01["in_emby"] is True
        assert e01["air_date"] == "2026-01-01"
        assert e01["season"] == 1 and e01["episode_number"] == 1

        e02 = by_ep["S01E02"]  # 旧 episode_state 遗留行（旧 DTO 同样带字段）
        assert e02["in_emby"] is False  # S01E02 不在 Emby 已有集
        assert e02["air_date"] == "2026-01-02"
        assert e02["season"] == 1 and e02["episode_number"] == 2


def test_detail_tv_emby_id_none_all_false(monkeypatch):
    """tv + tmdb_id 但 Emby 未收录（find_emby_id=None）→ in_emby 全 False；
    air_date 仍正常返回（TMDB 不受 Emby 收录影响）。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", AsyncMock(return_value={
        1: "2026-01-01", 2: "2026-01-02",
    }))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_dq_es)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert by_ep["S01E01"]["in_emby"] is False
        assert by_ep["S01E01"]["air_date"] == "2026-01-01"
        assert by_ep["S01E02"]["in_emby"] is False
        assert by_ep["S01E02"]["air_date"] == "2026-01-02"


# ---------------------------------------------------------------------------
# 2) Emby 故障降级：接口仍 200，in_emby 全 False
# ---------------------------------------------------------------------------

def test_detail_emby_failure_degrades(monkeypatch):
    """Emby 故障（find_emby_id 抛异常）→ 接口 200、in_emby 全 False、air_date 不受影响。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id",
                        AsyncMock(side_effect=RuntimeError("emby down")))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", AsyncMock(return_value={
        1: "2026-01-01", 2: "2026-01-02",
    }))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_dq_es)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert by_ep["S01E01"]["in_emby"] is False
        assert by_ep["S01E02"]["in_emby"] is False
        assert by_ep["S01E01"]["air_date"] == "2026-01-01"  # TMDB 路径不受影响


def test_detail_tmdb_season_failure_degrades(monkeypatch):
    """TMDB season 查询故障 → 接口 200、air_date 全 None、in_emby 不受影响。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[
        {"code": "S01E01"}]))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates",
                        AsyncMock(side_effect=RuntimeError("tmdb down")))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_dq_es)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert by_ep["S01E01"]["in_emby"] is True
        assert by_ep["S01E01"]["air_date"] is None
        assert by_ep["S01E02"]["air_date"] is None


# ---------------------------------------------------------------------------
# 3) movie：不触发查询逻辑，恒 False/None，接口 200
# ---------------------------------------------------------------------------

def test_detail_movie_no_tag_logic(monkeypatch):
    """movie（有 tmdb_id）→ 不调用 emby/tmdb 查询；in_emby=False、air_date=None，接口 200。"""
    from app.services import emby as emby_mod

    async def _explode(*args, **kwargs):
        raise AssertionError("movie 详情不应调用 emby/tmdb 查询逻辑")
    monkeypatch.setattr(emby_mod, "find_emby_id", _explode)
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", _explode)

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_movie)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert by_ep["S01E01"]["in_emby"] is False
        assert by_ep["S01E01"]["air_date"] is None


# ---------------------------------------------------------------------------
# 4) episode 非标准格式：in_emby=False、air_date=None，接口 200
# ---------------------------------------------------------------------------

def test_detail_non_standard_episode_tolerant(monkeypatch):
    """tv：episode 为非标准格式（全量模式文件名）→ season/episode_number 均为 None，
    in_emby=False、air_date=None，接口 200（不因解析失败报错）。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[
        {"code": "S01E01"}]))
    # 非标准集号行无 season → seasons 集合为空 → 不应调用 TMDB season
    async def _explode(*args, **kwargs):
        raise AssertionError("非标准集号不应触发 TMDB season 查询")
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", _explode)

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_non_standard_episode)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        e = r.json()["episode_state"][0]
        assert e["episode"] == "少帅将我宠上天.mp4"
        assert e["in_emby"] is False
        assert e["air_date"] is None
        assert e["season"] is None and e["episode_number"] is None


# ---------------------------------------------------------------------------
# 5) tmdb.get_tv_season_air_dates 单测（回源 / 非 200 / 缓存命中）
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, status_code=200, calls=None):
        self._payload = payload
        self._status_code = status_code
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None):
        if self._calls is not None:
            self._calls.append(url)
        return _FakeResp(self._payload, status_code=self._status_code)


def _make_http_factory(payload, status_code=200, calls=None):
    def factory(**kwargs):
        return _FakeClient(payload, status_code=status_code, calls=calls)
    return factory


def test_season_air_dates_fetch(monkeypatch):
    """回源正常：GET /3/tv/{id}/season/{n}，按 episode_number 装配（缺 air_date → None）。"""
    from app.config import settings
    from app.services import config_store

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    http_calls = []
    factory = _make_http_factory(
        {"episodes": [
            {"episode_number": 1, "air_date": "2026-01-01"},
            {"episode_number": 2, "air_date": None},
            {"episode_number": 3},  # 无 air_date → None
        ]},
        calls=http_calls,
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = __import__("asyncio").run(tmdb_mod.get_tv_season_air_dates(456, 1))
    assert result == {1: "2026-01-01", 2: None, 3: None}
    assert len(http_calls) == 1 and "/3/tv/456/season/1" in http_calls[0]


def test_season_air_dates_non_200_degrades(monkeypatch):
    """非 200 响应 → 降级返回 {}（不抛异常到调用方）。"""
    from app.config import settings
    from app.services import config_store

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    factory = _make_http_factory({"detail": "nope"}, status_code=500)
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    result = __import__("asyncio").run(tmdb_mod.get_tv_season_air_dates(456, 1))
    assert result == {}


def test_season_air_dates_cache_hit(monkeypatch):
    """进程内 TTL 缓存：第二次调用命中缓存，不再重新回源。"""
    from app.config import settings
    from app.services import config_store

    monkeypatch.setattr(settings, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config_store, "_cache", {})
    http_calls = []
    factory = _make_http_factory(
        {"episodes": [{"episode_number": 1, "air_date": "2026-01-01"}]},
        calls=http_calls,
    )
    monkeypatch.setattr("app.services.tmdb.httpx.AsyncClient", factory)

    async def _twice():
        first = await tmdb_mod.get_tv_season_air_dates(456, 1)
        # 第二次调用：若未命中缓存会再次发起回源（此时工厂已切为爆炸版）
        second = await tmdb_mod.get_tv_season_air_dates(456, 1)
        return first, second

    first, second = __import__("asyncio").run(_twice())
    assert first == {1: "2026-01-01"}
    assert second == {1: "2026-01-01"}
    assert len(http_calls) == 1  # 只回源一次，第二次命中缓存
