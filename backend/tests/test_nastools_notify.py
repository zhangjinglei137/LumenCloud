"""§十二 NaSTools Webhook 集成端点单测（POST /internal/nastools/notify）。

验证（对应 docs/影视下载两队列重设计.md §12.3）：
- token 正确（query / X-NaSTools-Token header / Authorization Bearer）→ 200 {"ok": true}
- token 缺失 / 错误 → 401；secret 未配置 → 503（fail-closed）
- body 非 JSON / 非对象 → 400
- transfer.finished → 推进该 media 的 scrape→library + 触发 library_check（fire-and-forget）
- transfer.fail / download.fail → flow_error 通知
- 其它事件 → 200 忽略

测试方式：仅挂载 nastools_notify.router 的最小 FastAPI app（无 lifespan），
TestClient 走真实 HTTP 层。DB 相关路径用 monkeypatch 替换模块内 async_session 为
fake sessionmaker（对齐 test_transfer.py 的 fake 风格）；library_check / notifier
为 AsyncMock。
"""
import json
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.nastools_notify as nn_mod
import app.tasks.library_check as lc_mod  # trigger_emby_refresh 的宿主模块（monkeypatch 用）

_TOKEN = "test-nastools-token-0123456789abcdef"
_ENDPOINT = "/internal/nastools/notify"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class FakeSession:
    """极简 fake session：get / execute / scalar 均可配。"""

    def __init__(self, get_result=None, execute_result=None):
        # get(实体, 键) → 直接返回配置结果
        self._get_result = get_result
        # execute(stmt) → 返回带 .first()/.scalars().first() 的假结果
        self._execute_result = execute_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        return self._get_result

    async def execute(self, stmt):
        return self._execute_result

    async def __call__(self):
        return self


class FakeRow:
    """execute 返回的「首行」：.first() 有值。"""

    def __init__(self, values: dict):
        self._values = values

    def first(self):
        return self

    def __getitem__(self, key):
        return self._values.get(key)


class FakeResult:
    """带 .first() / .scalars() 的假 execute 结果。"""

    def __init__(self, first_value):
        self._first_value = first_value
        self.rowcount = 0

    def first(self):
        return self._first_value

    class ScalarHolder:
        def __init__(self, val):
            self._val = val

        def first(self):
            return self._val

        def all(self):
            return [self._val] if self._val is not None else []

    def scalars(self):
        return FakeResult.ScalarHolder(self._first_value)


def make_client(secret: str | None, monkeypatch) -> TestClient:
    """构建最小 app；settings.NASTOOLS_WEBHOOK_SECRET 注入/清空（_secret 的 env fallback 源）。"""
    if secret is None:
        monkeypatch.setattr(nn_mod.settings, "NASTOOLS_WEBHOOK_SECRET", None)
    else:
        monkeypatch.setattr(nn_mod.settings, "NASTOOLS_WEBHOOK_SECRET", secret)
    app = FastAPI()
    app.include_router(nn_mod.router)
    return TestClient(app)


def make_media_row(media_id: int):
    """Media 查询结果（FakeRow）——execute(select(Media.id)).scalars().first() 的假值。"""
    return FakeRow({"id": media_id})


def media_payload(media_id: int | None = 12345, tmdb_id: int = 42):
    return {
        "type": "transfer.finished",
        "data": {
            "in_path": "/downloads/x",
            "file": "/downloads/x/movie.mkv",
            "target_path": "/media/movies/某某 (2024)",
            "dest": "/media/movies/某某 (2024)/某某 (2024).mkv",
            "media_info": {
                "tmdb_id": tmdb_id,
                "title": "某某",
                "media_type": "Movie",
                "year": "2024",
                "season": [],
                "episode": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def test_no_secret_configured_returns_503(monkeypatch):
    """secret 未配置 → 503（fail-closed）。"""
    cli = make_client(None, monkeypatch)
    resp = cli.post(_ENDPOINT, json={"type": "x"})
    assert resp.status_code == 503


@pytest.mark.parametrize("header_name,header_value", [
    ("X-NaSTools-Token", _TOKEN),
    ("Authorization", f"Bearer {_TOKEN}"),
])
def test_token_via_header_ok(monkeypatch, header_name, header_value):
    """header 带 token（新版消息通知→Webhook 渠道 / Authorization）→ 200。"""
    cli = make_client(_TOKEN, monkeypatch)
    media_row = make_media_row(42)
    async_session_fake = FakeSession(execute_result=FakeResult(media_row))

    monkeypatch.setattr(nn_mod, "async_session", lambda: async_session_fake)
    cli.app.state.nns = nn_mod
    resp = cli.post(_ENDPOINT, json={"type": "unrelated.event", "data": {}},
                    headers={header_name: header_value})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_token_via_query_ok(monkeypatch):
    """query ?token=（旧版插件 Webhook 地址带 token）→ 200。"""
    cli = make_client(_TOKEN, monkeypatch)
    media_row = make_media_row(42)
    monkeypatch.setattr(nn_mod, "async_session", lambda: FakeSession(execute_result=FakeResult(media_row)))
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json={"type": "unrelated.event", "data": {}})
    assert resp.status_code == 200


def test_token_missing_returns_401(monkeypatch):
    cli = make_client(_TOKEN, monkeypatch)
    resp = cli.post(_ENDPOINT, json={"type": "x"})
    assert resp.status_code == 401


def test_token_wrong_returns_401(monkeypatch):
    cli = make_client(_TOKEN, monkeypatch)
    resp = cli.post(f"{_ENDPOINT}?token=wrong", json={"type": "x"})
    assert resp.status_code == 401


def test_non_json_body_returns_400(monkeypatch):
    cli = make_client(_TOKEN, monkeypatch)
    resp = cli.post(
        f"{_ENDPOINT}?token={_TOKEN}",
        content=b"not-json", headers={"Content-Type": "text/plain"},
    )
    assert resp.status_code == 400


def test_non_object_body_returns_400(monkeypatch):
    cli = make_client(_TOKEN, monkeypatch)
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=[1, 2, 3])
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 事件分派
# ---------------------------------------------------------------------------

def test_transfer_finished_advances_and_triggers_library_check(monkeypatch):
    """transfer.finished → scrape→library 推进 + 后台触发 library_check + Emby 全库 Refresh。"""
    cli = make_client(_TOKEN, monkeypatch)
    # select(Media.id).scalars().first() → int（真实返回 media_id）
    async_session_fake = FakeSession(execute_result=FakeResult(88))
    monkeypatch.setattr(nn_mod, "async_session", lambda: async_session_fake)

    # 推进已作单元级 mock：只验证 _handle_transfer_finished 的分派（推进 + 后台触发）
    advanced_rec = {}

    async def fake_advance(media_id):
        advanced_rec["media_id"] = media_id
        return 1

    triggered = []

    async def fake_check_library_background(media_id):
        triggered.append(media_id)

    # Task 8：成功推进 → 同样触发 Emby 全库 Refresh（fire-and-forget）
    refresh_calls = []
    monkeypatch.setattr(lc_mod, "trigger_emby_refresh", lambda: refresh_calls.append(1))

    monkeypatch.setattr(nn_mod, "_advance_scrape_to_library", fake_advance)
    monkeypatch.setattr(nn_mod, "_check_library_background", fake_check_library_background)

    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=media_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["advanced"] == 1
    assert advanced_rec == {"media_id": 88}
    assert triggered == [88]
    assert refresh_calls == [1]


def test_transfer_finished_no_advance_skips_emby_refresh(monkeypatch):
    """无 scrape 任务可推进 → 不触发 Emby 全库 Refresh（避免无谓全库扫描）。"""
    cli = make_client(_TOKEN, monkeypatch)
    monkeypatch.setattr(nn_mod, "async_session", lambda: FakeSession(execute_result=FakeResult(88)))
    monkeypatch.setattr(nn_mod, "_advance_scrape_to_library", AsyncMock(return_value=0))
    refresh_calls = []
    monkeypatch.setattr(lc_mod, "trigger_emby_refresh", lambda: refresh_calls.append(1))

    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=media_payload())

    assert resp.status_code == 200
    assert resp.json()["advanced"] == 0
    assert refresh_calls == []


def test_transfer_finished_tmdb_not_in_library_ignored(monkeypatch):
    """tmdb_id 不在本系统 → 忽略（advanced=0）。"""
    cli = make_client(_TOKEN, monkeypatch)
    # Media 查询返回 None（FakeResult.first() → None）
    monkeypatch.setattr(nn_mod, "async_session", lambda: FakeSession(
        execute_result=FakeResult(None)))
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=media_payload(tmdb_id=999))
    assert resp.status_code == 200
    assert resp.json()["advanced"] == 0


def test_transfer_finished_missing_tmdb_ignored(monkeypatch):
    """payload 无 tmdb_id → 忽略不报错。"""
    cli = make_client(_TOKEN, monkeypatch)
    payload = media_payload()
    del payload["data"]["media_info"]["tmdb_id"]
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=payload)
    assert resp.status_code == 200
    assert resp.json()["advanced"] == 0


def test_transfer_fail_notifies_flow_error(monkeypatch):
    """transfer.fail → flow_error 通知。"""
    cli = make_client(_TOKEN, monkeypatch)
    notify_call = {}

    async def fake_notify(event):
        notify_call["event_type"] = event.event_type
        notify_call["title"] = event.title

    monkeypatch.setattr(nn_mod.notifier, "notify", fake_notify)
    payload = {"type": "transfer.fail", "data": {"media_info": {"title": "某某"}}}
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=payload)
    assert resp.status_code == 200
    assert notify_call.get("event_type") == "flow_error"


def test_unrelated_event_ignored(monkeypatch):
    """非关注事件 → 200 ok，不产生副作用。"""
    cli = make_client(_TOKEN, monkeypatch)
    # 若被误处理会查询 DB——用会抛异常的 async_session 证明未被调用
    def boom():
        raise AssertionError("不应访问 DB")
    monkeypatch.setattr(nn_mod, "async_session", boom)
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json={"type": "plugin.reload", "data": {}})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# fixture：无 DB 访问路径的 client（token 校验/非 JSON 分支）
@pytest.fixture()
def dbless_client(monkeypatch):
    return make_client(_TOKEN, monkeypatch)