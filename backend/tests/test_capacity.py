"""capacity 服务层单测：alist 递归统计、模型 B 判定、fail-closed 行为。

不连真实服务/数据库：mock alist.list_dir 与 provider 的持久化/配置读取方法。
"""
import asyncio
import types
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
import app.tasks.transfer as transfer_mod
from app.models import DownloadQueue, Media, QuarkCapacityLog
from app.routers.capacity import _get_usage
from app.services.alist import AlistUnavailable
from app.services import capacity as cap_mod
from app.services.capacity import CapacityInfo, CapacityProvider, CapacityUnavailable

GB = 1024 ** 3


def run(coro):
    return asyncio.run(coro)


def _fake_sessionmaker():
    """构造一个可控的 async_session mock，返回 (session_maker, session, commit_mock)。"""
    session = MagicMock()
    session.commit = AsyncMock()
    session_maker = MagicMock()
    session_maker.return_value.__aenter__ = AsyncMock(return_value=session)
    session_maker.return_value.__exit__ = AsyncMock(return_value=False)
    return session_maker, session


def _alist_patch(tree=None, fail_paths=()):
    """构造 alist.list_dir 的 mock：tree={path: entries}；fail_paths 里的目录抛异常。"""
    tree = tree or {}

    def fake(path):
        if path in (fail_paths or ()):
            raise AlistUnavailable(f"{path} 不可用")
        if path not in tree:
            raise AlistUnavailable(f"unexpected path {path}")
        return tree[path]

    return patch("app.services.capacity.alist.list_dir", new=AsyncMock(side_effect=fake))


# ---- 基础树：/quark → a(1G) + b(2G) + sub1；sub1 → c(4G) + sub2；sub2 → d(0.5G) ----
TREE = {
    "/quark": [
        {"name": "a.mkv", "is_dir": False, "size": 1 * GB},
        {"name": "b.mkv", "is_dir": False, "size": 2 * GB},
        {"name": "sub1", "is_dir": True, "size": 0},
    ],
    "/quark/sub1": [
        {"name": "c.mkv", "is_dir": False, "size": 4 * GB},
        {"name": "sub2", "is_dir": True, "size": 0},
    ],
    "/quark/sub1/sub2": [
        {"name": "d.srt", "is_dir": False, "size": int(0.5 * GB)},
    ],
}


def _patched_provider(provider, alist_patch):
    """构建已进入的 patch 上下文（ExitStack）：alist mock + settings 配置 + 快照 no-op。

    同时 mock _load_quota_gb 返回 env 配额（= provider._quota_gb）：本测试为纯单测，
    不依赖共享库 system_config 的 quark_quota_gb 状态（全量跑时 test_api_smoke 可能
    在共享库写入该配置，导致 info.total_gb 与 env 配额不一致而误失败）。
    """
    stack = ExitStack()
    for p in (
        alist_patch,
        patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
        patch.object(settings, "ALIST_TOKEN", "test-token"),
        patch.object(provider, "_persist_snapshot", new=AsyncMock()),
        patch.object(
            provider, "_load_quota_gb",
            new=AsyncMock(return_value=provider._quota_gb),
        ),
    ):
        stack.enter_context(p)
    return stack


# ---- get_usage：递归求和 ----

def test_get_usage_sums_files_recursively():
    async def scenario():
        provider = CapacityProvider()
        with _patched_provider(provider, _alist_patch(TREE)):
            info = await provider.get_usage()

        assert info.source == "alist"
        assert info.total_gb == pytest.approx(provider._quota_gb)
        assert info.used_gb == pytest.approx(7.5, abs=1e-6)  # 1+2+4+0.5 全量递归
        assert info.checked_at is not None
        return info

    run(scenario())


# ---- get_usage：单目录失败跳过其余 ----

def test_get_usage_skips_failed_subdir_but_keeps_others():
    async def scenario():
        provider = CapacityProvider()
        with _patched_provider(provider, _alist_patch(TREE, fail_paths=("/quark/sub1",))):
            info = await provider.get_usage()  # 不应抛（还有 /quark 及 sub2 成功路径）

        assert info.used_gb == pytest.approx(3.0, abs=1e-6)  # a(1G)+b(2G)，sub1 子树被跳过
        return info

    run(scenario())


def test_get_usage_raises_when_all_dirs_fail():
    async def scenario():
        provider = CapacityProvider()
        # 根目录也失败 → 一个目录都没成功 → fail-closed
        with _patched_provider(provider, _alist_patch({}, fail_paths=("/quark",))):
            with pytest.raises(CapacityUnavailable):
                await provider.get_usage()

    run(scenario())


# ---- get_usage：alist 未配置 / 网络故障 ----

def test_get_usage_raises_when_alist_not_configured():
    async def scenario():
        provider = CapacityProvider()
        with (
            patch.object(settings, "ALIST_BASE_URL", ""),
            patch.object(settings, "ALIST_TOKEN", ""),
        ):
            with pytest.raises(CapacityUnavailable):
                await provider.get_usage()

    run(scenario())


def test_get_usage_raises_when_network_error():
    async def scenario():
        provider = CapacityProvider()
        def boom(path):
            raise AlistUnavailable("网络故障")
        with (
            patch("app.services.capacity.alist.list_dir", new=AsyncMock(side_effect=boom)),
            patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
            patch.object(settings, "ALIST_TOKEN", "test-token"),
        ):
            with pytest.raises(CapacityUnavailable):
                await provider.get_usage()

    run(scenario())


# ---- get_usage：成功写入 quark_capacity_log 快照（真实 _persist_snapshot） ----

def test_get_usage_persists_snapshot():
    async def scenario():
        provider = CapacityProvider()
        # 重置模块级节流时间戳，保证本测试必然写库
        cap_mod._last_snapshot_written_at = 0.0
        session_maker, session = _fake_sessionmaker()
        with (
            _alist_patch(TREE),
            patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
            patch.object(settings, "ALIST_TOKEN", "test-token"),
            patch("app.services.capacity.async_session", session_maker),
        ):
            await provider.get_usage()

        assert session.commit.await_count == 1
        added_log = session.add.call_args.args[0]
        assert isinstance(added_log, QuarkCapacityLog)
        assert added_log.source == "alist"
        assert added_log.total_gb == pytest.approx(provider._quota_gb)
        assert added_log.used_gb == pytest.approx(7.5, abs=1e-6)
        assert added_log.checked_at is not None
        return added_log

    run(scenario())


# ---- 快照节流：60s 内不重复写 ----

def test_snapshot_persist_throttled_within_60s():
    async def scenario():
        provider = CapacityProvider()
        cap_mod._last_snapshot_written_at = 0.0
        session_maker, session = _fake_sessionmaker()
        with patch("app.services.capacity.async_session", session_maker):
            snap = CapacityInfo(total_gb=10.0, used_gb=1.0, source="alist")
            await provider._persist_snapshot(snap)
            await provider._persist_snapshot(snap)  # 60s 内 → 节流跳过

        assert session.commit.await_count == 1

    run(scenario())


def test_snapshot_persist_failure_does_not_raise():
    async def scenario():
        provider = CapacityProvider()
        cap_mod._last_snapshot_written_at = 0.0

        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def add(self, obj):
                raise RuntimeError("写库炸了")

            async def commit(self):
                pass

        maker = MagicMock(return_value=FakeSession())
        with patch("app.services.capacity.async_session", maker):
            await provider._persist_snapshot(CapacityInfo(total_gb=10, used_gb=1.0, source="alist"))
        # 写库失败仅告警，不抛 → 主流程不受影响

    run(scenario())


# ---- check：模型 B 判定（含余量边界） ----

def test_check_model_b_allow_and_boundary():
    async def scenario():
        with patch.object(settings, "QUARK_QUOTA_GB", 10.0):
            provider = CapacityProvider()
            quota = provider._quota_gb  # 10.0（已固定，防 .env 覆盖）
            margin = quota * 0.05  # 0.5
            used_gb = 7.5  # 用 TREE 的统计结果

            stack = ExitStack()
            for p in (
                _alist_patch(TREE),
                patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
                patch.object(settings, "ALIST_TOKEN", "test-token"),
                patch.object(provider, "_persist_snapshot", new=AsyncMock()),
                # P1-2a 后 quota 运行时读 system_config（测试顺序下共享库可能被
                # test_api_smoke 的 PATCH 污染），此处固定为 env 值以聚焦判定逻辑
                patch.object(provider, "_load_quota_gb", new=AsyncMock(return_value=quota)),
                patch.object(provider, "_load_margin_gb", new=AsyncMock(return_value=margin)),
            ):
                stack.enter_context(p)
            with stack:
                # 边界：used + candidate + margin == quota → 放行（相等算允许）
                boundary_bytes = int((quota - used_gb - margin) * GB)
                assert await provider.check(boundary_bytes) is True

                # 超出余量 1 字节 → 拒绝
                assert await provider.check(boundary_bytes + 1) is False

                # 明显超出 → 拒绝
                assert await provider.check(int(5 * GB)) is False
        return quota, margin

    run(scenario())


def test_check_uses_margin_from_config_key():
    async def scenario():
        with patch.object(settings, "QUARK_QUOTA_GB", 10.0):
            provider = CapacityProvider()

            stack = ExitStack()
            for p in (
                _alist_patch(TREE),
                patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
                patch.object(settings, "ALIST_TOKEN", "test-token"),
                patch.object(provider, "_persist_snapshot", new=AsyncMock()),
                # P1-2a 后 quota 运行时读 system_config（测试顺序下共享库可能被
                # test_api_smoke 的 PATCH 污染），此处固定为 env 值以聚焦 margin 配置
                patch.object(provider, "_load_quota_gb", new=AsyncMock(return_value=provider._quota_gb)),
                patch.object(provider, "_load_margin_gb", new=AsyncMock(return_value=1.0)),
            ):
                stack.enter_context(p)
            with stack:
                # used=7.50G（TREE 递归统计） + 1.5G + margin=1.0G = 10.0G <= quota(10) → 放行（边界）
                assert await provider.check(int(1.5 * GB)) is True
                # used=7.50G + ~1.51G + 1.0G > quota → 拒绝
                assert await provider.check(int(1.51 * GB)) is False

    run(scenario())


def test_check_raises_when_capacity_unavailable():
    async def scenario():
        provider = CapacityProvider()
        def boom(path):
            raise AlistUnavailable("全部失败")
        with (
            patch("app.services.capacity.alist.list_dir", new=AsyncMock(side_effect=boom)),
            patch.object(settings, "ALIST_BASE_URL", "http://alist.test"),
            patch.object(settings, "ALIST_TOKEN", "test-token"),
        ):
            with pytest.raises(CapacityUnavailable):
                await provider.check(GB)  # check 如实上抛 → 调用方 fail-closed

    run(scenario())


# ---- margin 默认值：配置缺失 → quota × 5% ----

def test_load_margin_default_when_config_missing():
    async def scenario():
        provider = CapacityProvider()
        session = MagicMock()
        session.get = AsyncMock(return_value=None)  # system_config 键不存在
        session_maker = MagicMock()
        session_maker.return_value.__aenter__ = AsyncMock(return_value=session)
        session_maker.return_value.__exit__ = AsyncMock(return_value=False)
        with patch("app.services.capacity.async_session", session_maker):
            margin = await provider._load_margin_gb()

        assert margin == pytest.approx(provider._quota_gb * 0.05)

    run(scenario())


def test_load_margin_uses_config_value():
    async def scenario():
        provider = CapacityProvider()
        session = MagicMock()
        session.get = AsyncMock(return_value=MagicMock(value="2.5"))
        session_maker = MagicMock()
        session_maker.return_value.__aenter__ = AsyncMock(return_value=session)
        session_maker.return_value.__exit__ = AsyncMock(return_value=False)
        with patch("app.services.capacity.async_session", session_maker):
            margin = await provider._load_margin_gb()

        assert margin == pytest.approx(2.5)

    run(scenario())


# ---- C-3：/api/capacity 流程复用模块级单例 provider（吃到缓存与快照节流）----

def test_router_get_usage_reuses_module_singleton():
    """C-3：_get_usage 走模块级单例 provider.get_usage，返回其 source/total_gb/used_gb。"""
    async def scenario():
        checked = datetime.now(timezone.utc)
        answer = CapacityInfo(total_gb=100.0, used_gb=12.5, source="alist", checked_at=checked)
        # patch 单例方法 → _get_usage 内引用的 provider 是同一对象，必然命中
        with patch.object(cap_mod.provider, "get_usage", new=AsyncMock(return_value=answer)):
            result = await _get_usage()

        assert result["source"] == "alist"
        assert result["total_gb"] == pytest.approx(100.0)
        assert result["used_gb"] == pytest.approx(12.5)
        assert result["checked_at"] is checked

    run(scenario())


def test_router_get_usage_unavailable_returns_explicit_null():
    """Q3：容量不可用（CapacityUnavailable）→ unavailable dict 显式含
    total_gb=None / used_gb=None（前端 JSON 契约稳定，非缺失或字符串）。"""
    async def scenario():
        with patch.object(
            cap_mod.provider, "get_usage",
            new=AsyncMock(side_effect=CapacityUnavailable("alist down")),
        ):
            result = await _get_usage()

        assert result["source"] == "unavailable"
        assert result["error"] == "容量数据不可用"
        assert result["total_gb"] is None
        assert result["used_gb"] is None

    run(scenario())


# ---------------------------------------------------------------------------
# queue-flow-rework Task 6：事件触发下载队列消费（容量释放续跑 + 并发防重入）
# ---------------------------------------------------------------------------
# 入库完成释放容量后触发续跑：消费入口 trigger_transfer_consume 唤醒 quota_wait 并
# 准入取件；多事件并发触发时 asyncio.Lock 防重入（同一时刻只跑一轮消费）。


def _task6_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _Task6Aria2:
    """aria2.client：GID 校验（tell_active/tell_waiting 空）+ add_uri。"""

    def __init__(self):
        self.add_uri_calls = []

    async def tell_active(self):
        return []

    async def tell_waiting(self):
        return []

    async def add_uri(self, uri, **kwargs):
        self.add_uri_calls.append((uri, kwargs))
        return "gid-1"


class _Task6CloudSaver:
    def __init__(self):
        self.save_calls = []

    async def save(self, params):
        self.save_calls.append(dict(params))
        return {"task_id": "t1"}


class _Task6Alist:
    async def remove(self, names, dir):
        return {"success": True}

    async def get_link(self, path):
        return "http://alist.test/raw/ep.mkv"

    async def list_dir(self, path):
        return [{"name": "ep.mkv", "is_dir": False, "size": 123}]


class _Task6Capacity:
    def __init__(self):
        self.check_calls = []
        self.invalidate_calls = 0

    async def check(self, candidate_bytes):
        self.check_calls.append(candidate_bytes)
        return True  # mock 容量充足（模拟入库完成释放容量后）

    def invalidate_usage_cache(self):
        self.invalidate_calls += 1


class _Task6Notifier:
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
def task6_env(monkeypatch):
    """fake 外部服务（准入门 mock 容量充足），替换 transfer 模块依赖引用。"""
    fakes = {
        "aria2": _Task6Aria2(),
        "cloudsaver": _Task6CloudSaver(),
        "alist": _Task6Alist(),
        "capacity": _Task6Capacity(),
        "notifier": _Task6Notifier(),
    }
    monkeypatch.setattr(transfer_mod, "aria2", types.SimpleNamespace(client=fakes["aria2"]))
    monkeypatch.setattr(transfer_mod, "cloudsaver", fakes["cloudsaver"])
    monkeypatch.setattr(transfer_mod, "alist", fakes["alist"])
    monkeypatch.setattr(transfer_mod, "capacity", types.SimpleNamespace(provider=fakes["capacity"]))
    monkeypatch.setattr(transfer_mod, "notifier", fakes["notifier"])
    return fakes


async def _seed_quota_wait(db, *, episode="S01E01"):
    """media(tracking) + download_queue(quota_wait，容量等待中)。返回 (mid, dq_id)。"""
    async with db() as s:
        media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
        s.add(media)
        await s.flush()
        dq = DownloadQueue(
            media_id=media.id, episode=episode, file_name="ep.mkv", file_size=1024,
            share_code="sc123", stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status="quota_wait", wait_since=_task6_now(),
            retry_count=0, node_attempt=0, enqueued_at=_task6_now(), updated_at=_task6_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return media.id, dq.id


async def _read_dq(db, dq_id):
    async with db() as s:
        return await s.get(DownloadQueue, dq_id)


def test_consume_trigger_resumes_quota_wait_and_admits(db, task6_env, monkeypatch):
    """入库完成释放容量后触发续跑：quota_wait 行 + mock 容量充足 → 调用消费入口
    trigger_transfer_consume → quota_wait 唤醒回 pending → 被取件转入存（downloading）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)
    mid, dq_id = run(_seed_quota_wait(db))

    run(transfer_mod.trigger_transfer_consume())

    dq = run(_read_dq(db, dq_id))
    assert dq.status == "downloading"  # quota_wait → pending（释放唤醒）→ transferring → downloading（被取件）
    assert dq.wait_since is None        # 准入成功后清除等待起点
    assert len(task6_env["capacity"].check_calls) == 1  # 真实准入路径询问过容量（mock 充足）
    assert len(task6_env["cloudsaver"].save_calls) == 1  # 被取件后正常转存


def test_consume_trigger_lock_prevents_concurrent_rounds():
    """事件触发并发防重入（asyncio.Lock）：第一轮消费进行中时，第二轮触发直接跳过，
    同一时刻只允许一轮 _admit_batch 在跑。"""
    from unittest.mock import patch

    calls = []

    async def slow_admit_batch():
        calls.append("enter")
        await asyncio.sleep(0.05)
        calls.append("exit")

    async def scenario():
        with patch.object(transfer_mod, "_admit_batch", new=slow_admit_batch):
            t1 = asyncio.create_task(transfer_mod.trigger_transfer_consume())
            await asyncio.sleep(0.01)  # t1 已进入消费并持锁
            t2 = asyncio.create_task(transfer_mod.trigger_transfer_consume())
            await t1
            await t2

    run(scenario())

    assert calls == ["enter", "exit"]  # 第二轮触发被防重入跳过，未再进入消费
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# fix-transfer-flow-reliability Task 10：容量记账漏计窗口（design T7）
# 准入提交成功后使 used 缓存失效（downloading 落盘立即反映真实 used）
# ---------------------------------------------------------------------------

def test_invalidate_usage_cache_forces_recount():
    """invalidate_usage_cache 后 get_usage 重新统计（__new__ 绕过 __init__，模拟已有缓存）。

    design T7：转存提交成功（落盘 downloading）后立即使 30s 进程内 used 缓存失效，
    下一轮准入 re-count 反映真实 used（downloading 仍不计 reserved，防双计）。
    仅在具名测试中预置缓存字段，不走 __init__（避免依赖 settings/外部构造）。
    """
    provider = cap_mod.CapacityProvider.__new__(cap_mod.CapacityProvider)
    provider._fallback_quota_gb = 100.0
    provider._usage_cache = object()    # 模拟已有未过期缓存
    provider._usage_cached_at = 9999.0  # 未过期（monotonic 不可能达到）

    provider.invalidate_usage_cache()

    assert provider._usage_cache is None
    assert provider._usage_cached_at == 0.0


def test_commit_downloading_invalidates_cache(db, task6_env, monkeypatch):
    """_commit_downloading 成功路径（'admitted'）调用 capacity.provider.invalidate_usage_cache：
    文件已落盘 downloading，下一轮准入立即反映真实 used（消除漏计窗口）。"""
    monkeypatch.setattr(transfer_mod, "async_session", db)

    async def _seed_transferring():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=None, status="tracking")
            s.add(media)
            await s.flush()
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="a.mkv", file_size=1,
                share_code="sc", stoken="stoken-x", receive_code="提取码占位",
                fids='["f1"]', fid_tokens='["ft1"]', folder_id="folder-1",
                status="transferring",
                enqueued_at=_task6_now(), updated_at=_task6_now(),
            )
            s.add(dq)
            await s.flush()
            await s.commit()
            return media.id, dq.id

    mid, dq_id = run(_seed_transferring())

    result = run(transfer_mod._commit_downloading(
        dq_id, mid, "S01E01", "a.mkv", "out.mkv", "gid-1", None, 0.0,
    ))

    assert result == "admitted"
    assert task6_env["capacity"].invalidate_calls == 1