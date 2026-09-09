"""海报代理端点单测（路由函数直调，user 传 fake；鉴权在 test_poster_auth.py）。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.poster as poster_mod
from app.routers import poster as poster_router

_USER = SimpleNamespace(id=1, role="user", username="u")


def test_router_rejects_invalid_path(monkeypatch):
    """端点对非法路径返回 400，且不发起对外请求。"""
    fetch = AsyncMock()
    monkeypatch.setattr(poster_mod, "fetch_poster", fetch)

    async def _run():
        return await poster_router.get_poster(p="/t/p/../../etc/passwd", user=_USER)

    resp = asyncio.run(_run())
    assert resp.status_code == 400
    fetch.assert_not_awaited()