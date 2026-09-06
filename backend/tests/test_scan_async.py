"""E-1（P1）：scan 异步化（fire-and-forget）单测。

覆盖扫码触发入口的异步化行为：
- POST /api/media/{id}/scan（scan_media_route）：不再同步 await 完整巡检，
  立即返回 {"ok": True, "task_run_id": None}，后台触发走 trigger_scan_background；
- trigger_scan_background 未就绪（ImportError）→ 兜底返回 ok + 告警（既有契约）。

全部通过 monkeypatch 隔离，不触碰真实 DB / 外部网络；路由函数直接调用（admin
注入 SimpleNamespace，参考 test_fix_online 绕过 Depends 的注入方式）。
approve 侧的火力覆盖在 test_approval_dup.py（已断言批准后同步记录器收到新
media 的 id），此处不重复。
"""
import asyncio
import types

import pytest
from unittest.mock import MagicMock

import app.tasks.scan as scan_mod  # noqa: F401  提供 trigger_scan_background patch 目标
from app.routers.media import scan_media_route

# admin.id 不会落库——MagicMock 即可；id 用真实 int 保持一致
AKA = types.SimpleNamespace(id=1)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1) scan_media_route：fire-and-forget 立即返回 + 后台触发收到 media_id
# ---------------------------------------------------------------------------

def test_scan_media_route_fire_and_forget(monkeypatch):
    """手动触发立即返回 ok（task_run_id=None），后台触发同步记录器收到 (media_id,)。"""
    trigger_calls: list[int] = []
    monkeypatch.setattr(
        "app.tasks.scan.trigger_scan_background",
        lambda media_id: trigger_calls.append(media_id),
    )

    res = run(scan_media_route(7, admin=AKA))

    assert res == {"ok": True, "task_run_id": None}
    assert trigger_calls == [7]


# ---------------------------------------------------------------------------
# 2) scan_media_route：trigger_scan_background 未就绪 → 兜底 ok
# ---------------------------------------------------------------------------

def test_scan_media_route_trigger_unready_fallback(monkeypatch):
    """延迟导入抛 ImportError（触发函数不存在）→ 仍返回 ok（无副作用，契约不回退）。"""
    monkeypatch.delattr("app.tasks.scan.trigger_scan_background", raising=False)

    res = run(scan_media_route(7, admin=MagicMock()))

    assert res == {"ok": True, "task_run_id": None}