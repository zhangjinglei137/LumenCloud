"""tasks 层清理与公共化单测（cleanup / 公共化回归）。

覆盖本次「tasks 层清理与公共化」改动：
1. transfer._alert_cooldown TTL 清理（任务 3）：注入过期/未过期条目 → 触发
   _record_alert 入口后过期项被清、未过期项保留；同窗口内同 bucket 不重复 notify
   （P2-2 节流语义不回归，conftest 每测试 clear 的重置语义不受影响）。
2. scan._scan_lock_idle 保守回退（任务 2）：锁无 _waiters 属性（CPython 私有
   属性变化）→ 返回 False 且不抛异常；真实锁有/无等待者判定正确。
3. logs.title contains 通配符（任务 6）：title 参数含字面 "%" 只命中含该字面
   子串的 media（不复用 ilike 拼接造成的通配符注入）。
4. utils 公共函数（任务 1）：now_utc_naive 返回 naive UTC（tzinfo None 且与
   timezone.utc 时刻一致）；split_quark_path 与 transfer 行为一致。
5. 回归（任务 2-5）：各文件从 utils 导入后模块可正常 import，绑定指向 utils。

风格：in-memory SQLite（StaticPool，参照 test_admin_users.py）+ 直接调用路由函数
绕过 Depends；_record_alert 用 MagicMock/AsyncMock 伪造 async_session 与 notifier。
"""
import asyncio
import time
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import Media, TaskRun
from app.routers.logs import list_logs
from app.services.notifier import EVENT_FLOW_ERROR
import app.tasks.library_check as library_check_mod
import app.tasks.recovery as recovery_mod
import app.tasks.scan as scan_mod
import app.tasks.transfer as transfer_mod
import app.utils as utils


def run(coro):
    return asyncio.run(coro)


# 当前登录用户（仅用于绕过 list_logs 的 Depends(get_current_admin)）
ADMIN = SimpleNamespace(id=1, role="admin", username="boss")


@pytest.fixture()
def db():
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


def _patch_record_alert_deps(monkeypatch):
    """伪造 _record_alert 依赖（async_session + notifier），返回 notifier mock。

    _record_alert 内部：async with async_session() as s → record_task_run + commit；
    末尾 notifier.notify。record_task_run 只做 session.add/flush（MagicMock 接受），
    commit 需要 AsyncMock。
    """
    session = MagicMock()
    session.commit = AsyncMock()
    session.flush = AsyncMock()  # record_task_run 内部 await session.flush()
    maker = MagicMock()
    maker.return_value.__aenter__ = AsyncMock(return_value=session)
    maker.return_value.__exit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(transfer_mod, "async_session", maker)
    notifier_mock = MagicMock()
    notifier_mock.notify = AsyncMock()
    monkeypatch.setattr(transfer_mod, "notifier", notifier_mock)
    return notifier_mock


# ---------------------------------------------------------------------------
# 1) transfer._alert_cooldown TTL 清理
# ---------------------------------------------------------------------------

def test_alert_cooldown_ttl_purges_stale_entries(monkeypatch):
    """注入超 2 倍窗口的过期条目 → 触发 _record_alert 后被清理；未过期保留。"""
    notifier_mock = _patch_record_alert_deps(monkeypatch)
    old_ts = time.monotonic() - 3 * transfer_mod._ALERT_COOLDOWN_SECONDS
    fresh_ts = time.monotonic() - transfer_mod._ALERT_COOLDOWN_SECONDS / 2
    transfer_mod._alert_cooldown["1:capacity"] = (old_ts, "capacity")
    transfer_mod._alert_cooldown["2:capacity"] = (fresh_ts, "capacity")

    run(transfer_mod._record_alert(None, "新告警", category="capacity", bucket="capacity"))

    # 过期条目（不可能再被命中）被清理防泄漏；未过期条目保留
    assert "1:capacity" not in transfer_mod._alert_cooldown
    assert "2:capacity" in transfer_mod._alert_cooldown
    # 本次触发本身走「新告警」分支 → notify 发出
    assert notifier_mock.notify.call_count == 1


def test_alert_cooldown_same_window_same_bucket_no_dup_notify(monkeypatch):
    """同窗口内相同 bucket 重复告警不重复 notify（P2-2 节流语义不回归）。"""
    notifier_mock = _patch_record_alert_deps(monkeypatch)
    # 首次触发 → 必须通知并写入节流时间戳
    run(transfer_mod._record_alert(1, "容量不足", category="capacity", bucket="capacity"))
    assert notifier_mock.notify.call_count == 1
    assert "1:capacity" in transfer_mod._alert_cooldown
    # 窗口内（10 分钟）同 bucket 相同消息 → 节流，不重复 notify
    run(transfer_mod._record_alert(1, "容量不足", category="capacity", bucket="capacity"))
    assert notifier_mock.notify.call_count == 1


def test_alert_cooldown_different_message_inside_window_still_notifies(monkeypatch):
    """同窗口但 bucket 不同（根因变化）→ 视为新告警照常通知（M4 语义不回归）。"""
    notifier_mock = _patch_record_alert_deps(monkeypatch)
    run(transfer_mod._record_alert(1, "容量不足", category="capacity", bucket="capacity"))
    run(transfer_mod._record_alert(1, "GID 校验失败", category="gid", bucket="gid"))
    assert notifier_mock.notify.call_count == 2


# ---------------------------------------------------------------------------
# 2) scan._scan_lock_idle 保守回退
# ---------------------------------------------------------------------------

def test_scan_lock_idle_false_when_waiters_unreadable():
    """锁对象无 _waiters 属性（CPython 私有属性变化）→ 保守 False 且不抛异常。"""
    lock = types.SimpleNamespace()  # 无 _waiters → 读取抛 AttributeError
    assert scan_mod._scan_lock_idle(lock) is False
    assert scan_mod._scan_lock_idle(lock) is False  # 可重复调用不抛


def test_scan_lock_idle_true_when_no_waiters():
    """真实锁、无等待者（_waiters 为 None）→ True（可安全移除）。"""
    lock = asyncio.Lock()
    assert scan_mod._scan_lock_idle(lock) is True


def test_scan_lock_idle_false_when_waiters_present():
    """真实锁、有等待者 → False（保留锁，防「新锁+旧锁」并发巡检）。"""
    lock = asyncio.Lock()

    async def _scenario():
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)  # 让 waiter 进入 _waiters 队列
        assert scan_mod._scan_lock_idle(lock) is False
        lock.release()
        await waiter

    run(_scenario())


# ---------------------------------------------------------------------------
# 3) logs.title contains 通配符（不复用 ilike 拼接注入）
# ---------------------------------------------------------------------------

def test_logs_title_contains_matches_literal_percent(db):
    """title 参数含字面 '%' → 只命中 title 含该字面子串的 media（不含 '%' 的不命中）。"""
    async def _seed():
        async with db() as s:
            m1 = Media(title="Attack a% Dawn", media_type="tv")
            m2 = Media(title="Attack at Dawn", media_type="tv")
            s.add_all([m1, m2])
            await s.flush()
            s.add(TaskRun(task_type="scan_media", media_id=m1.id, status="success", message="a"))
            s.add(TaskRun(task_type="scan_media", media_id=m2.id, status="success", message="b"))
            await s.commit()

    run(_seed())

    async def _query():
        async with db() as s:
            # 直接调用绕过 Depends：Query(...) 默认参数不会自动解析，须全部显式传值
            return await list_logs(
                admin=ADMIN, session=s,
                task_type=None, status=None, media_id=None, tmdb_id=None,
                title="a%", limit=50, offset=0,
            )

    rows = run(_query())
    titles = [r["media_title"] for r in rows]
    # contains 自动转义 % → 只按字面 "a%" 匹配，含该字面的命中、不含的不命中
    assert "Attack a% Dawn" in titles
    assert "Attack at Dawn" not in titles


# ---------------------------------------------------------------------------
# 4) utils 公共函数
# ---------------------------------------------------------------------------

def test_now_utc_naive_is_naive_utc():
    """now_utc_naive 返回 naive（tzinfo is None）且与 timezone.utc 时刻一致。"""
    dt = utils.now_utc_naive()
    assert dt.tzinfo is None
    aware = dt.replace(tzinfo=timezone.utc)
    assert abs((aware - datetime.now(timezone.utc)).total_seconds()) < 5


def test_split_quark_path_matches_transfer_behavior():
    """split_quark_path 行为与原 transfer._split_quark_path 一致（边界覆盖）。

    注意：`/quark/` 这类「目录型」路径原实现拆为 ("//", ["quark"])（rstrip 后
    rsplit 得 dir_part=""，`(dir_part or "/") + "/"` 拼出 "//"）——忠实保留原
    逻辑不变（实际 quark_path 均为 `/quark/<file>` 形态）。
    """
    assert utils.split_quark_path("/quark/movie.mkv") == ("/quark/", ["movie.mkv"])
    assert utils.split_quark_path("/quark/") == ("//", ["quark"])  # 忠实原实现
    assert utils.split_quark_path("/quark/a/b/c.mkv") == ("/quark/a/b/", ["c.mkv"])
    assert utils.split_quark_path("plain.mkv") == ("/", ["plain.mkv"])
    assert utils.split_quark_path("") == ("/", [])
    assert utils.split_quark_path(None) == ("/", [])
    # 与 transfer 的同一函数（公共化后绑定一致）
    assert transfer_mod._split_quark_path is utils.split_quark_path


def test_fmt_episode_and_parse_episode_num():
    """fmt_episode / parse_episode_num 语义与原 scan/library_check 实现一致。"""
    assert utils.fmt_episode(1, 1) == "S01E01"
    assert utils.fmt_episode(1, 100) == "S01E100"
    assert utils.parse_episode_num("S01E10") == 10
    assert utils.parse_episode_num("S01E100") == 100
    assert utils.parse_episode_num("abc") is None
    assert utils.parse_episode_num("") is None
    # 与 scan / library_check 绑定一致
    assert scan_mod._fmt_episode is utils.fmt_episode
    assert library_check_mod._fmt_episode is utils.fmt_episode
    assert scan_mod._ep_num is utils.parse_episode_num
    assert library_check_mod._ep_num is utils.parse_episode_num


# ---------------------------------------------------------------------------
# 5) 回归：各文件从 utils 导入后模块可正常 import / 绑定正确
# ---------------------------------------------------------------------------

def test_modules_import_and_use_utils_bindings():
    """各模块从 utils 导入后正常可用，_now / 拆分函数绑定指向 utils。"""
    assert scan_mod._now is utils.now_utc_naive
    assert transfer_mod._now is utils.now_utc_naive
    assert recovery_mod._now is utils.now_utc_naive
    assert library_check_mod._now is utils.now_utc_naive
    assert recovery_mod._split_quark_path is utils.split_quark_path
