"""episode-status-cache Task 4：详情合并视图 + 列表 total 种子回归测试。

背景（reviewer Important-1）：合并视图核心交付（「详情集数状态真实展示」）此前
仅靠 /tmp 一次性脚本验证，未提交任何可复现测试；既有 media 测试种子无
tmdb_id/缓存，全部走回退路径。本文件补齐真实验收覆盖：

1. get_media 合并视图：TMDB 全集轴（episode_info_cache 经**真实**
   get_episode_info 读取→缓存优先不触发回源）＋本地 download_queue 状态合并
   （done→in_library、failed→error、无本地行按 air_date 落 not_aired/pending）、
   name 取轴数据、file_size 取本地真实值、tmdb_episodes 顶层字段兼容；
2. list_media episode_stats.total：TmdbCache.number_of_episodes 优先（10 > 聚合 2）。

数据库：独立 in-memory SQLite（StaticPool 共享连接），fixture 模式同
test_library_check.py 的 db；不经过 TestClient/lifespan，直接调用路由函数。
外部服务全部打桩：emby.find_emby_id → None（无 Emby 收录）、
tmdb.get_tv_season_air_dates → {}（免回源）；tmdb_mod.async_session 替换为
in-memory maker（tmdb.py 注释约定：monkeypatch tmdb_mod.async_session 生效），
使真实 get_episode_info 读到种子缓存行。
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_epstatus_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 隔离外部服务：即便某个未打桩分支尝试网络调用也直接放行降级，不发起真实请求
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
import app.models  # noqa: F401,E402  注册全部 ORM 模型
from app.models import (  # noqa: E402
    DownloadQueue,
    EpisodeInfoCache,
    Media,
    TmdbCache,
    User,
)
from app.routers.media import get_media, list_media  # noqa: E402
from app.services import emby as emby_mod  # noqa: E402
from app.services import tmdb as tmdb_mod  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


_TMDB_ID = 777

# TMDB 全集轴：10 集（与 TmdbCache.number_of_episodes=10 一致）。
# 已开播 E01/E02/E05/E08；未开播（未来首播）E03/E06/E09；无首播日 E04/E07/E10。
_EPISODE_INFO: list[dict] = [
    {"season": 1, "episode": n, "name": f"第{n}集", "air_date": air}
    for n, air in enumerate(
        [
            "2026-01-01", "2026-01-02", "2027-01-01", None, "2026-03-01",
            "2027-03-01", None, "2026-05-01", "2027-05-01", None,
        ],
        start=1,
    )
]


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


@pytest.fixture()
def isolate_db(monkeypatch, db):
    """替换 media 路由的外部服务依赖并返回回源桩：真实 get_episode_info 读
    in-memory 缓存；Emby 不收录；season air_date 免回源；get_tv_all_episodes
    打桩并断言其未被调用（缓存优先）。"""
    monkeypatch.setattr(tmdb_mod, "async_session", db)  # 真实 get_episode_info 读 in-memory 缓存
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", AsyncMock(return_value={}))
    fallback = AsyncMock(return_value=[])
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fallback)
    return fallback


async def _seed(db):
    """media(tv) + TmdbCache(10) + EpisodeInfoCache 10 集 + download_queue
    （done S01E01 / failed S01E02）。"""
    now = _now()
    async with db() as s:
        media = Media(title="合并视图种子剧", media_type="tv", tmdb_id=_TMDB_ID, status="tracking")
        s.add(media)
        await s.flush()
        s.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        s.add(
            DownloadQueue(
                media_id=media.id, episode="S01E02", file_name="f02.mkv",
                file_size=2 * 1024**3, share_code="ScBb2222", status="failed",
                enqueued_at=now, updated_at=now,
            )
        )
        s.add(
            TmdbCache(
                tmdb_id=str(_TMDB_ID), title="合并视图种子剧", media_type="tv",
                number_of_episodes=10,
            )
        )
        for ep in _EPISODE_INFO:
            s.add(
                EpisodeInfoCache(
                    tmdb_id=_TMDB_ID, season=ep["season"], episode=ep["episode"],
                    name=ep["name"], air_date=ep["air_date"],
                )
            )
        await s.commit()
        return media.id


def test_detail_merged_view_full_tmdb_axis(isolate_db, db):
    """详情合并视图（原 /tmp 一次性验证转正）：全集轴 10 集，本地 merged 按
    (season, episode) 合入；done→in_library、failed→error、未开播→not_aired、
    无信息→pending；name 取轴数据、file_size 取本地真实值；tmdb_episodes 顶层
    兼容；缓存优先未触发回源 get_tv_all_episodes。"""
    fallback = isolate_db
    media_id = run(_seed(db))
    admin = User(role="admin", username="admin", password_hash="x")

    async def _call():
        async with db() as s:
            return await get_media(media_id, admin, s)

    detail = run(_call())
    eps = detail["episode_state"]
    by_ep = {e["episode"]: e for e in eps}

    # 全集轴：按 season/episode 升序全量 10 集（含无本地记录的集）
    assert [e["episode"] for e in eps] == [f"S01E{i:02d}" for i in range(1, 11)]

    # 本地 done → in_library；name 取自轴（缓存）；file_size 取本地真实值
    e01 = by_ep["S01E01"]
    assert e01["state"] == "in_library" and e01["status"] == "in_library"
    assert e01["name"] == "第1集"
    assert e01["file_size"] == 1024**3 and e01["size_gb"] == 1.0
    assert e01["season"] == 1 and e01["episode_number"] == 1

    # 本地 failed → error（异常优先于巡检中）；file_size 取本地失败行真实值
    e02 = by_ep["S01E02"]
    assert e02["state"] == "error"
    assert e02["name"] == "第2集"
    assert e02["file_size"] == 2 * 1024**3

    # 无本地行：未来首播 → not_aired；无首播日 → pending；已开播但无本地 → pending
    assert by_ep["S01E03"]["state"] == "not_aired"
    assert by_ep["S01E04"]["state"] == "pending"
    assert by_ep["S01E05"]["state"] == "pending"
    assert by_ep["S01E09"]["state"] == "not_aired"

    # 无 Emby 收录 → in_emby 全 False
    assert all(not e["in_emby"] for e in eps)

    # tmdb_episodes 顶层兼容字段
    assert len(detail["tmdb_episodes"]) == 10

    # 缓存优先：真实 get_episode_info 命中缓存，未回源 get_tv_all_episodes
    fallback.assert_not_awaited()


def test_list_episode_stats_total_prefers_tmdb_count(db):
    """列表 episode_stats.total 优先 TMDB 全集数（10），回退逻辑 max 兜底
    （三表聚合 2）；done=1（dq done），missing=9。"""
    media_id = run(_seed(db))
    admin = User(role="admin", username="admin", password_hash="x")

    async def _call():
        async with db() as s:
            rows = await list_media(admin, s)
        return next(m for m in rows if m["id"] == media_id)

    m = run(_call())
    assert m["episode_stats"] == {
        "total": 10, "done": 1, "failed": 1, "in_progress": 0,
        "available": 1, "downloaded": 1, "missing": 9,
    }