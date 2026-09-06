"""capacity 服务层单测：alist 递归统计、模型 B 判定、fail-closed 行为。

不连真实服务/数据库：mock alist.list_dir 与 provider 的持久化/配置读取方法。
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.models import QuarkCapacityLog
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