"""L3 library_check（入库轮询）+ scrape_runner（刮削执行器）单测。

全部使用 fake 依赖（monkeypatch app.tasks.library_check 模块内的
nastools_sync/emby/alist/notifier/async_session），不连任何真实外部服务/数据库。
数据库用独立 in-memory SQLite（StaticPool 共享连接）。

验证场景：
- 刮削执行器：node='scrape' 存在 → nastools_sync(force=True) 成功 → 全部推进 library；
  同步抛异常 → node_attempt++（<3 保持 scrape 重试 / ≥3 failed + node_error）；无待刮削 → 空跑；
  P2-2：node='scrape' 但 state 非 downloading 的异常残留不采集不推进
- 入库轮询：Emby 命中 → node='done'+state='done'+删夸克+「入库完成」通知+media 回退+续跑；
  P1-2 集级确认：剧集当前集在遗漏集 → 不 finalize 保持等待；get_missing_episodes 异常 → 跳过；
  movie 命中 → 直接 finalize（不查遗漏集）；P2-5 真实名匹配删除；
  未命中超时（默认 600s / system_config 覆盖）→ failed + 超时诊断 + P1-6 清理夸克；
  未超时继续等；Emby 故障 → 本轮跳过不误判；media 不存在 → 清理解除
- P2-8：emby.list_library 分页拉取全部（Limit=500 满页按 StartIndex 翻页）
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.library_check as library_check_mod
import app.tasks.transfer as transfer_mod
from app.models import EpisodeState, Media, SystemConfig
from app.services import config_store as cs
from app.services import emby as emby_mod
from app.services.emby import EmbyUnavailable
from app.services.notifier import EVENT_DOWNLOAD_COMPLETE


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FakeAlist:
    def __init__(self):
        self.remove_calls = []  # [(names, dir)]
        self.list_dir_calls = []  # [path]
        self.dir_entries = []  # list_dir 返回的目录条目（P2-5 真实名匹配用）

    async def remove(self, names, dir):
        self.remove_calls.append((list(names), dir))
        return {"success": True}

    async def list_dir(self, path, per_page=1000):
        self.list_dir_calls.append(path)
        return list(self.dir_entries)


class FakeNotifier:
    def __init__(self):
        self.events = []

    async def notify(self, event):
        self.events.append(event)


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
def env(monkeypatch):
    """全套 fake 服务 + 替换 library_check 模块内的依赖引用。"""
    spawn_calls = []
    fakes = {
        "alist": FakeAlist(),
        "notifier": FakeNotifier(),
        "nastools": types.SimpleNamespace(nastools_sync=AsyncMock(return_value=None)),
        "emby": types.SimpleNamespace(
            find_emby_id=AsyncMock(return_value=None),
            # P1-2：剧集入库确认的遗漏集查询默认返回空（当前集不在遗漏 → 可 finalize）
            get_missing_episodes=AsyncMock(return_value=[]),
        ),
        "spawn_calls": spawn_calls,
    }
    monkeypatch.setattr(library_check_mod, "alist", fakes["alist"])
    monkeypatch.setattr(library_check_mod, "notifier", fakes["notifier"])
    monkeypatch.setattr(library_check_mod, "nastools_sync", fakes["nastools"])
    monkeypatch.setattr(library_check_mod, "emby", fakes["emby"])
    # P2-6：_spawn 续跑记录不真实创建后台任务（防 asyncio.run 退出时挂起未完成任务）
    monkeypatch.setattr(library_check_mod, "_spawn", lambda f: spawn_calls.append(f))
    return fakes


def patch_db(monkeypatch, db):
    monkeypatch.setattr(library_check_mod, "async_session", db)
    # transfer._sync_media_status / _split_quark_path 用 transfer 命名空间的 async_session
    monkeypatch.setattr(transfer_mod, "async_session", db)


# ---------------------------------------------------------------------------
# 种子数据 / 读取
# ---------------------------------------------------------------------------

async def seed_scrape(db, *, episode="S01E01", node_attempt=0):
    """media(downloading) + es(node='scrape')。返回 (mid, es_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="downloading")
        s.add(media)
        await s.flush()
        es = EpisodeState(
            media_id=media.id, episode=episode, state="downloading", node="scrape",
            file_name="ep.mkv", file_size=1024, share_code="sc123",
            quark_path="/quark/ep.mkv", node_attempt=node_attempt,
            node_started_at=_now(), node_finished_at=_now(), updated_at=_now(),
        )
        s.add(es)
        await s.flush()
        await s.commit()
        return media.id, es.id


async def seed_library(db, *, episode="S01E01", started_at=None, media_status="downloading",
                       tmdb_id=42, media_type="tv", quark_path="/quark/ep.mkv"):
    """media + es(node='library'，state 保持 downloading)。返回 (mid, es_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type=media_type, tmdb_id=tmdb_id, status=media_status)
        s.add(media)
        await s.flush()
        es = EpisodeState(
            media_id=media.id, episode=episode, state="downloading", node="library",
            file_name="ep.mkv", file_size=1024, share_code="sc123",
            quark_path=quark_path, node_attempt=0,
            node_started_at=started_at or _now(), updated_at=_now(),
        )
        s.add(es)
        await s.flush()
        await s.commit()
        return media.id, es.id


async def get_es(db, es_id):
    async with db() as s:
        return await s.get(EpisodeState, es_id)


async def get_media(db, media_id):
    async with db() as s:
        return await s.get(Media, media_id)


async def set_timeout_config(db, seconds: int):
    async with db() as s:
        s.add(SystemConfig(key="library_check_timeout_seconds", value=str(seconds)))
        await s.commit()


# ---------------------------------------------------------------------------
# 刮削执行器（scrape → library / 失败计数）
# ---------------------------------------------------------------------------

def test_scrape_success_promotes_es_to_library(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_scrape(db))

    run(library_check_mod.scrape_runner())

    # nastools_sync(force=True) 被调用（跳过冷却）
    env["nastools"].nastools_sync.assert_awaited_once_with(force=True)
    es = run(get_es(db, es_id))
    assert es.node == "library"
    assert es.node_attempt == 0
    assert es.node_started_at is not None
    assert es.node_finished_at is not None
    assert es.node_error is None
    assert es.state == "downloading"  # 刮削/入库阶段 state 保持进行中（不置 done）


def test_scrape_skips_when_no_pending(db, env, monkeypatch):
    """无 node='scrape' 的集 → 空跑，不触发 Nastools 同步（job 每 30s tick 零开销）。"""
    patch_db(monkeypatch, db)
    run(library_check_mod.scrape_runner())
    env["nastools"].nastools_sync.assert_not_awaited()


def test_scrape_failure_counts_attempt_and_fails_at_limit(db, env, monkeypatch):
    """Nastools 同步抛异常 → node_attempt++（<3 保持 scrape 重试 / ≥3 failed 终态）。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_scrape(db))
    env["nastools"].nastools_sync = AsyncMock(side_effect=RuntimeError("NasTools 不可用"))

    # 第 1、2 次失败 → node_attempt 1/2，保持 scrape（同节点重试，node_error 写异常详情）
    for expect in (1, 2):
        run(library_check_mod.scrape_runner())
        es = run(get_es(db, es_id))
        assert es.node == "scrape"
        assert es.state == "downloading"
        assert es.node_attempt == expect
        assert "NasTools 不可用" in (es.node_error or "")

    # 第 3 次失败 → node_attempt=3 ≥ 上限 → failed 终态
    run(library_check_mod.scrape_runner())
    es = run(get_es(db, es_id))
    assert es.node == "failed"
    assert es.state == "failed"
    assert es.node_attempt == 3
    assert "NasTools 不可用" in (es.node_error or "")
    # 终态后 media 无其他进行中集 → 回退 tracking
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# 入库轮询（library → done / failed / 继续等 / 清理）
# ---------------------------------------------------------------------------

def test_library_hit_marks_done_and_removes_quark(db, env, monkeypatch):
    """Emby 命中（剧集：当前集不在遗漏集）→ done+删夸克+通知+media 回退+续跑。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[])

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "done"
    assert es.state == "done"
    assert es.node_finished_at is not None
    assert es.node_error is None
    assert env["emby"].find_emby_id.await_count == 1
    assert env["emby"].get_missing_episodes.await_count == 1  # 剧集做了集级确认
    # 夸克中转文件已删除（G6：入库确认后释放；P2-5 列目录匹配不到 → 回退原始名）
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert env["alist"].list_dir_calls == ["/quark"]
    # 「入库完成」通知（复用 download_complete 事件类型，文案区分）
    done_events = [e for e in env["notifier"].events if e.event_type == EVENT_DOWNLOAD_COMPLETE]
    assert len(done_events) == 1
    assert "入库完成" in done_events[0].title
    assert done_events[0].extra["media_id"] == mid
    assert done_events[0].extra["episode"] == "S01E01"
    # media 无其他进行中集 → 回退 tracking（P3-6）
    assert run(get_media(db, mid)).status == "tracking"
    # P2-6：入库完成触发转存续跑（_spawn(process_transfer_queue)）
    assert env["spawn_calls"] == [transfer_mod.process_transfer_queue]

    # 幂等：再跑一轮不重复删/通知（node 已 done，不再命中轮询）
    run(library_check_mod.library_check())
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert len([e for e in env["notifier"].events if e.event_type == EVENT_DOWNLOAD_COMPLETE]) == 1


def test_library_timeout_marks_failed(db, env, monkeypatch):
    """Emby 未收录且超时（node_started_at + 600s < now）→ node='failed' + 超时诊断。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "failed"
    assert es.state == "failed"
    assert es.node_error == "入库超时：Emby 未收录，请人工核实刮削配置"
    # P1-6：超时 failed 后 best-effort 清理夸克中转文件（释放中转空间）
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"  # 终态后 media 回退


def test_library_not_expired_keeps_waiting(db, env, monkeypatch):
    """Emby 未收录且未超时 → 保持 node='library'，继续等下一轮，不动数据。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db))  # started_at=now，未超时
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "library"
    assert es.state == "downloading"
    assert es.node_error is None
    assert env["alist"].remove_calls == []


def test_library_timeout_respects_config(db, env, monkeypatch):
    """system_config 键 library_check_timeout_seconds 覆盖默认超时。"""
    patch_db(monkeypatch, db)
    run(set_timeout_config(db, 10))
    mid, es_id = run(seed_library(db, started_at=_now() - timedelta(seconds=30)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "failed"
    assert "入库超时" in (es.node_error or "")


def test_library_emby_error_skips_round_even_expired(db, env, monkeypatch):
    """find_emby_id 抛异常（Emby 故障）→ 本轮跳过不误判。

    超时判定基于 node_started_at；即便节点已超时，Emby 故障轮次也不消耗窗口、
    不产生副作用（继续等，等 Emby 恢复后下轮正常判定）。
    """
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(side_effect=EmbyUnavailable("Emby 请求超时"))

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "library"  # 不误判 failed
    assert es.node_error is None
    assert env["alist"].remove_calls == []


def test_library_missing_media_cleans_up(db, env, monkeypatch):
    """media 已被删除 → 直接清理解除：删孤儿 es 行 + best-effort 删夸克文件。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db))

    async def del_media():
        async with db() as s:
            await s.execute(delete(Media).where(Media.id == mid))
            await s.commit()

    run(del_media())

    run(library_check_mod.library_check())

    assert run(get_es(db, es_id)) is None  # es 行被清除
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]  # 文件一并清理
    env["emby"].find_emby_id.assert_not_awaited()  # 无需查 Emby


# ---------------------------------------------------------------------------
# P1-2 集级入库确认（剧集：当前集在遗漏集 → 不 finalize / movie 直接 finalize）
# ---------------------------------------------------------------------------

def test_library_tv_episode_still_missing_waits(db, env, monkeypatch):
    """剧集 find_emby_id 命中但当前集仍在 Emby 遗漏集 → 不 finalize，保持等待。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, episode="S01E10"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "library"  # 未被误判 done
    assert es.state == "downloading"
    assert es.node_error is None
    # 未 finalize → 不删夸克、不通知、不续跑
    assert env["alist"].remove_calls == []
    assert env["notifier"].events == []
    assert env["spawn_calls"] == []
    assert run(get_media(db, mid)).status == "downloading"  # media 不误回退


def test_library_tv_filename_episode_in_missing_waits(db, env, monkeypatch):
    """episode 为文件名（含 SxxExx）且该集在遗漏集 → 同样不 finalize（参照 match_missing 提取）。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, episode="剧名.S01E10.1080p.mkv"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "library"
    assert env["alist"].remove_calls == []


def test_library_tv_missing_episodes_error_skips(db, env, monkeypatch):
    """get_missing_episodes 抛异常（Emby 故障）→ 本轮跳过不误判（不 finalize）。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(
        side_effect=EmbyUnavailable("Emby 请求失败")
    )

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "library"  # 不误判 done / failed
    assert es.node_error is None
    assert env["alist"].remove_calls == []
    assert env["notifier"].events == []


def test_library_movie_hit_finalizes_without_episode_check(db, env, monkeypatch):
    """电影（media_type='movie'）无集级概念：find_emby_id 命中即 finalize，不查遗漏集。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, episode="电影.mkv", media_type="movie",
                                  quark_path="/quark/电影.mkv"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-movie-1")

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "done"
    assert es.state == "done"
    env["emby"].get_missing_episodes.assert_not_awaited()  # movie 不做集级确认
    assert env["alist"].remove_calls == [(["电影.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# P2-5 真实名匹配删除 / P1-6 超时清理
# ---------------------------------------------------------------------------

def test_library_removes_quark_real_name(db, env, monkeypatch):
    """P2-5：夸克实际文件名与 quark_path 不一致（规范化改名）→ 按真实名删除。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db))
    env["alist"].dir_entries = [{"name": "EP.MKV", "is_dir": False, "size": 1024}]
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[])

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "done"
    # 按 list_dir 返回的真实名删除（大小写归一化匹配），而非原始 quark_path 名
    assert env["alist"].remove_calls == [(["EP.MKV"], "/quark/")]


def test_library_timeout_remove_failure_still_marks_failed(db, env, monkeypatch):
    """P1-6：超时 failed 后删夸克失败 → 仅告警不阻塞（failed 已落库）。"""
    patch_db(monkeypatch, db)
    mid, es_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    async def boom(names, dir):
        raise RuntimeError("alist 挂")

    env["alist"].remove = boom

    run(library_check_mod.library_check())

    es = run(get_es(db, es_id))
    assert es.node == "failed"  # 删除失败不影响终态判定
    assert es.state == "failed"
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# P2-2 刮削执行器只推进正常流转集（state='downloading'）
# ---------------------------------------------------------------------------

def test_scrape_only_promotes_downloading_state(db, env, monkeypatch):
    """node='scrape' 但 state 非 downloading（异常残留）→ 不采集、不批量推进。"""
    patch_db(monkeypatch, db)
    mid1, es1 = run(seed_scrape(db, episode="S01E01"))  # 正常：state=downloading

    async def seed_stale():
        # 异常残留：node='scrape' 但 state='queued'
        async with db() as s:
            media2 = Media(title="测试剧2", media_type="tv", tmdb_id=43, status="tracking")
            s.add(media2)
            await s.flush()
            es2 = EpisodeState(
                media_id=media2.id, episode="S01E02", state="queued", node="scrape",
                file_name="ep2.mkv", file_size=1024, share_code="sc456",
                quark_path="/quark/ep2.mkv", node_attempt=0,
                node_started_at=_now(), node_finished_at=_now(), updated_at=_now(),
            )
            s.add(es2)
            await s.flush()
            await s.commit()
            return es2.id

    es2_id = run(seed_stale())

    run(library_check_mod.scrape_runner())

    es1_row = run(get_es(db, es1))
    assert es1_row.node == "library"  # 正常集被推进
    es2_row = run(get_es(db, es2_id))
    assert es2_row.node == "scrape"  # 异常残留未被推进
    assert es2_row.state == "queued"


# ---------------------------------------------------------------------------
# P2-8 emby.list_library 分页拉取全部（>500 条不截断）
# ---------------------------------------------------------------------------

def _emby_movie_item(i):
    return {
        "Id": f"m{i}",
        "Name": f"Movie{i}",
        "Type": "Movie",
        "ProviderIds": {"Tmdb": str(10000 + i)},
        "ProductionYear": 2020,
        "ImageTags": {"Primary": "p"},
    }


def test_list_library_paginates_all_items(db, monkeypatch):
    """list_library 分页：Limit=500 满页时按 StartIndex 翻页直至尾页（含上限防御）。"""
    patch_db(monkeypatch, db)
    # 两满页（500×2）+ 尾页空 → 3 次 /Items 请求
    pages = {
        0: [_emby_movie_item(i) for i in range(500)],
        500: [_emby_movie_item(i) for i in range(500, 1000)],
        1000: [],
    }
    item_starts = []

    async def fake_get(path, params, timeout=None):
        if path == "/System/Info/Public":
            return {"Id": "srv-1"}
        start = int(params.get("StartIndex", "0"))
        item_starts.append(start)
        return {"Items": pages.get(start, [])}

    monkeypatch.setattr(emby_mod, "_get", fake_get)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })

    result = run(emby_mod.list_library())

    assert item_starts == [0, 500, 1000]  # 满页翻页 → 空尾页停止
    assert len(result) == 1000  # 全量返回，不被 Limit=500 截断
    assert result[0]["title"] == "Movie0"
    assert result[999]["title"] == "Movie999"