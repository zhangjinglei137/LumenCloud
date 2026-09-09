"""L3 library_check（入库轮询）+ scrape_runner（刮削执行器）单测（download_queue 版）。

全部使用 fake 依赖（monkeypatch app.tasks.library_check 模块内的
nastools_sync/emby/alist/notifier/async_session），不连任何真实外部服务/数据库。
数据库用独立 in-memory SQLite（StaticPool 共享连接）。

操作对象：download_queue 单表（status='scrape' / status='library'，承接旧 episode_state
node 维度职责）。

验证场景：
- 刮削执行器：status='scrape' 存在 → nastools_sync(force=True) 成功 → 全部推进 library；
  同步抛异常 → node_attempt++（<3 保持 scrape 重试 / ≥3 failed + node_error）；无待刮削 → 空跑；
  status 非 scrape（终态/排队残留）不采集不推进
- 入库轮询：Emby 命中 → status='done' + 删夸克 + 「入库完成」通知 + media 回退 + 续跑；
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
from app.models import DownloadQueue, Media, SystemConfig
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
    """media(downloading) + download_queue(status='scrape')。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="downloading")
        s.add(media)
        await s.flush()
        dq = DownloadQueue(
            media_id=media.id, episode=episode, status="scrape",
            file_name="ep.mkv", file_size=1024, share_code="sc123",
            quark_path="/quark/ep.mkv", node_attempt=node_attempt,
            node_started_at=_now(), node_finished_at=_now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return media.id, dq.id


async def seed_library(db, *, episode="S01E01", started_at=None, media_status="downloading",
                       tmdb_id=42, media_type="tv", quark_path="/quark/ep.mkv"):
    """media + download_queue(status='library'，node_started_at 可指定超时场景)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type=media_type, tmdb_id=tmdb_id, status=media_status)
        s.add(media)
        await s.flush()
        dq = DownloadQueue(
            media_id=media.id, episode=episode, status="library",
            file_name="ep.mkv", file_size=1024, share_code="sc123",
            quark_path=quark_path, node_attempt=0,
            node_started_at=started_at or _now(), updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return media.id, dq.id


async def get_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


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

def test_scrape_success_promotes_to_library(db, env, monkeypatch):
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_scrape(db))

    run(library_check_mod.scrape_runner())

    # nastools_sync(force=True) 被调用（跳过冷却）
    env["nastools"].nastools_sync.assert_awaited_once_with(force=True)
    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"
    assert dq.node_attempt == 0
    assert dq.node_started_at is not None
    assert dq.node_finished_at is not None
    assert dq.node_error is None


def test_scrape_skips_when_no_pending(db, env, monkeypatch):
    """无 status='scrape' 的任务 → 空跑，不触发 Nastools 同步（job 每 30s tick 零开销）。"""
    patch_db(monkeypatch, db)
    run(library_check_mod.scrape_runner())
    env["nastools"].nastools_sync.assert_not_awaited()


def test_scrape_failure_counts_attempt_and_fails_at_limit(db, env, monkeypatch):
    """Nastools 同步抛异常 → node_attempt++（<3 保持 scrape 重试 / ≥3 failed 终态）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_scrape(db))
    env["nastools"].nastools_sync = AsyncMock(side_effect=RuntimeError("NasTools 不可用"))

    # 第 1、2 次失败 → node_attempt 1/2，保持 scrape（同节点重试，node_error 写异常详情）
    for expect in (1, 2):
        run(library_check_mod.scrape_runner())
        dq = run(get_dq(db, dq_id))
        assert dq.status == "scrape"
        assert dq.node_attempt == expect
        assert "NasTools 不可用" in (dq.node_error or "")

    # 第 3 次失败 → node_attempt=3 ≥ 上限 → failed 终态
    run(library_check_mod.scrape_runner())
    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"
    assert dq.node_attempt == 3
    assert "NasTools 不可用" in (dq.node_error or "")
    # 终态后 media 无其他进行中集 → 回退 tracking
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# 入库轮询（library → done / failed / 继续等 / 清理）
# ---------------------------------------------------------------------------

def test_library_hit_marks_done_and_removes_quark(db, env, monkeypatch):
    """Emby 命中（剧集：当前集不在遗漏集）→ done+删夸克+通知+media 回退+续跑。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "done"
    assert dq.node_finished_at is not None
    assert dq.node_error is None
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
    # Task 6：入库完成触发下载队列消费续跑（_spawn(trigger_transfer_consume)，
    # 事件消费入口：唤醒 quota_wait + 取件 + 有界准入，防重入锁在 transfer 侧）
    assert env["spawn_calls"] == [transfer_mod.trigger_transfer_consume]

    # 幂等：再跑一轮不重复删/通知（status 已 done，不再命中轮询）
    run(library_check_mod.library_check())
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert len([e for e in env["notifier"].events if e.event_type == EVENT_DOWNLOAD_COMPLETE]) == 1


def test_library_timeout_marks_failed(db, env, monkeypatch):
    """Emby 未收录且超时（node_started_at + 600s < now）→ status='failed' + 超时诊断。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"
    assert dq.node_error == "入库超时：Emby 未收录，请人工核实刮削配置"
    # P1-6：超时 failed 后 best-effort 清理夸克中转文件（释放中转空间）
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"  # 终态后 media 回退


def test_library_not_expired_keeps_waiting(db, env, monkeypatch):
    """Emby 未收录且未超时 → 保持 status='library'，继续等下一轮，不动数据。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))  # started_at=now，未超时
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"
    assert dq.node_error is None
    assert env["alist"].remove_calls == []


def test_library_timeout_respects_config(db, env, monkeypatch):
    """system_config 键 library_check_timeout_seconds 覆盖默认超时。"""
    patch_db(monkeypatch, db)
    run(set_timeout_config(db, 10))
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=30)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"
    assert "入库超时" in (dq.node_error or "")


def test_library_emby_error_skips_round_even_expired(db, env, monkeypatch):
    """find_emby_id 抛异常（Emby 故障）→ 本轮跳过不误判。

    超时判定基于 node_started_at；即便节点已超时，Emby 故障轮次也不消耗窗口、
    不产生副作用（继续等，等 Emby 恢复后下轮正常判定）。
    """
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(side_effect=EmbyUnavailable("Emby 请求超时"))

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 不误判 failed
    assert dq.node_error is None
    assert env["alist"].remove_calls == []


def test_library_missing_media_cleans_up(db, env, monkeypatch):
    """media 已被删除 → 直接清理解除：删孤儿 download_queue 行 + best-effort 删夸克文件。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))

    async def del_media():
        async with db() as s:
            await s.execute(delete(Media).where(Media.id == mid))
            await s.commit()

    run(del_media())

    run(library_check_mod.library_check())

    assert run(get_dq(db, dq_id)) is None  # download_queue 行被清除
    assert env["alist"].remove_calls == [(["ep.mkv"], "/quark/")]  # 文件一并清理
    env["emby"].find_emby_id.assert_not_awaited()  # 无需查 Emby


# ---------------------------------------------------------------------------
# P1-2 集级入库确认（剧集：当前集在遗漏集 → 不 finalize / movie 直接 finalize）
# ---------------------------------------------------------------------------

def test_library_tv_episode_still_missing_waits(db, env, monkeypatch):
    """剧集 find_emby_id 命中但当前集仍在 Emby 遗漏集 → 不 finalize，保持等待。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, episode="S01E10"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 未被误判 done
    assert dq.node_error is None
    # 未 finalize → 不删夸克、不通知、不续跑
    assert env["alist"].remove_calls == []
    assert env["notifier"].events == []
    assert env["spawn_calls"] == []
    assert run(get_media(db, mid)).status == "downloading"  # media 不误回退


def test_library_tv_filename_episode_in_missing_waits(db, env, monkeypatch):
    """episode 为文件名（含 SxxExx）且该集在遗漏集 → 同样不 finalize（参照 match_missing 提取）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, episode="剧名.S01E10.1080p.mkv"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[
        {"code": "S01E10", "season": 1, "episode": 10, "name": "E10"},
    ])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"
    assert env["alist"].remove_calls == []


def test_library_tv_missing_episodes_error_skips(db, env, monkeypatch):
    """get_missing_episodes 抛异常（Emby 故障）→ 本轮跳过不误判（不 finalize）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(
        side_effect=EmbyUnavailable("Emby 请求失败")
    )

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "library"  # 不误判 done / failed
    assert dq.node_error is None
    assert env["alist"].remove_calls == []
    assert env["notifier"].events == []


def test_library_movie_hit_finalizes_without_episode_check(db, env, monkeypatch):
    """电影（media_type='movie'）无集级概念：find_emby_id 命中即 finalize，不查遗漏集。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, episode="电影.mkv", media_type="movie",
                                  quark_path="/quark/电影.mkv"))
    env["emby"].find_emby_id = AsyncMock(return_value="emby-movie-1")

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "done"
    env["emby"].get_missing_episodes.assert_not_awaited()  # movie 不做集级确认
    assert env["alist"].remove_calls == [(["电影.mkv"], "/quark/")]
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# P2-5 真实名匹配删除 / P1-6 超时清理
# ---------------------------------------------------------------------------

def test_library_removes_quark_real_name(db, env, monkeypatch):
    """P2-5：夸克实际文件名与 quark_path 不一致（规范化改名）→ 按真实名删除。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db))
    env["alist"].dir_entries = [{"name": "EP.MKV", "is_dir": False, "size": 1024}]
    env["emby"].find_emby_id = AsyncMock(return_value="emby-1")
    env["emby"].get_missing_episodes = AsyncMock(return_value=[])

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "done"
    # 按 list_dir 返回的真实名删除（大小写归一化匹配），而非原始 quark_path 名
    assert env["alist"].remove_calls == [(["EP.MKV"], "/quark/")]


def test_library_timeout_remove_failure_still_marks_failed(db, env, monkeypatch):
    """P1-6：超时 failed 后删夸克失败 → 仅告警不阻塞（failed 已落库）。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_library(db, started_at=_now() - timedelta(seconds=700)))
    env["emby"].find_emby_id = AsyncMock(return_value=None)

    async def boom(names, dir):
        raise RuntimeError("alist 挂")

    env["alist"].remove = boom

    run(library_check_mod.library_check())

    dq = run(get_dq(db, dq_id))
    assert dq.status == "failed"  # 删除失败不影响终态判定
    assert run(get_media(db, mid)).status == "tracking"


# ---------------------------------------------------------------------------
# 刮削执行器只推进 scrape 态任务（防误碰终态/排队残留）
# ---------------------------------------------------------------------------

def test_scrape_only_promotes_scrape_status(db, env, monkeypatch):
    """status='scrape' 的任务被批量推进；终态/排队残留（非 scrape）不采集不推进。"""
    patch_db(monkeypatch, db)
    mid1, dq1 = run(seed_scrape(db, episode="S01E01"))  # 正常：status='scrape'

    async def seed_stale():
        # 残留：status='failed'（终态），绝不能被动 scrape 逻辑
        async with db() as s:
            media2 = Media(title="测试剧2", media_type="tv", tmdb_id=43, status="tracking")
            s.add(media2)
            await s.flush()
            dq2 = DownloadQueue(
                media_id=media2.id, episode="S01E02", status="failed",
                file_name="ep2.mkv", file_size=1024, share_code="sc456",
                quark_path="/quark/ep2.mkv", node_attempt=3,
                node_started_at=_now(), node_finished_at=_now(),
                error="历史失败", updated_at=_now(),
            )
            s.add(dq2)
            await s.flush()
            await s.commit()
            return dq2.id

    dq2_id = run(seed_stale())

    run(library_check_mod.scrape_runner())

    assert run(get_dq(db, dq1)).status == "library"  # scrape 集被推进
    assert run(get_dq(db, dq2_id)).status == "failed"  # 终态残留未被推进


# ---------------------------------------------------------------------------
# Task 8：刮削成功 → 触发 Emby 全库 Refresh（trigger_emby_refresh）
# ---------------------------------------------------------------------------

def test_scrape_success_triggers_emby_refresh(db, env, monkeypatch):
    """刮削成功推进 scrape→library 后，fire-and-forget 触发 Emby 全库 Refresh。"""
    patch_db(monkeypatch, db)
    mid, dq_id = run(seed_scrape(db))

    run(library_check_mod.scrape_runner())

    # env 夹具把 _spawn monkeypatch 为记录调用 → 收到 _emby_refresh_impl（fire-and-forget）
    assert env["spawn_calls"] == [library_check_mod._emby_refresh_impl]


def test_emby_refresh_mutex_skip_and_failure_tolerant(db, env, monkeypatch):
    """全库扫描互斥（锁占用时跳过）+ refresh_library 失败仅告警不抛异常（轮询兜底）。"""
    patch_db(monkeypatch, db)
    fired = []

    async def scenario():
        # 1) refresh_library 抛异常 → 不向调用方抛（Emby 收录由 library_check 轮询兜底）
        env["emby"].refresh_library = AsyncMock(side_effect=RuntimeError("Emby 挂"))
        await library_check_mod._emby_refresh_impl()

        # 2) 锁被占用 → 本轮跳过（不重复全库扫描）
        env["emby"].refresh_library = AsyncMock(side_effect=lambda: fired.append(1))
        async with library_check_mod._emby_refresh_lock:
            await library_check_mod._emby_refresh_impl()

        # 3) 锁释放 → 正常触发一次
        await library_check_mod._emby_refresh_impl()

    run(scenario())
    assert fired == [1]  # 仅第 3 步真正触发


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


# ---------------------------------------------------------------------------
# Task 5：入库完成（_finalize_done）同步 episode_state 双表（集数统计联动）
# ---------------------------------------------------------------------------

def test_finalize_done_syncs_episode_state(db, env, monkeypatch):
    """finalize 后 download_queue=done 且 episode_state 双表一致（state='done'）。"""
    from app.models import EpisodeState
    from app.tasks.library_check import _finalize_done

    patch_db(monkeypatch, db)
    # media(downloading) + download_queue(status='library', file_size 有值)
    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=77, status="downloading")
            s.add(media)
            await s.commit()
            await s.refresh(media)
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", status="library",
                file_name="剧.S01E01.mkv", file_size=2 * 1024 ** 3,
                share_code="sc123", quark_path="/quark/a.mkv",
                node_started_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return media.id, dq.id
    mid, dq_id = run(seed())

    async def do_finalize():
        await _finalize_done(dq_id, mid, "S01E01", "剧.S01E01.mkv", "/quark/a.mkv", transfer_mod)
    run(do_finalize())

    async def assert_state():
        async with db() as s:
            dq = (await s.execute(select(DownloadQueue).where(DownloadQueue.id == dq_id))).scalar_one()
            es = (await s.execute(select(EpisodeState).where(
                EpisodeState.media_id == mid, EpisodeState.episode == "S01E01",
            ))).scalar_one_or_none()
            return dq.status, es
    dq_status, es = run(assert_state())
    assert dq_status == "done"
    assert es is not None and es.state == "done"
    assert es.file_size == 2 * 1024 ** 3