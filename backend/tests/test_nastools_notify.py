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
import asyncio
import json
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import DownloadQueue, Media
import app.routers.nastools_notify as nn_mod
import app.tasks.library_check as lc_mod  # trigger_emby_refresh 的宿主模块（monkeypatch 用）

_TOKEN = "test-nastools-token-0123456789abcdef"
_ENDPOINT = "/internal/nastools/notify"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture()
def db():
    """独立 in-memory SQLite（StaticPool 共享连接）→ 返回 sessionmaker（对齐 test_transfer.py）。"""
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


async def seed_media_and_scrape(db, *, tmdb_id=42, title="测试剧", rows):
    """写入 media + 若干 download_queue(scrape) 行。rows: [(episode, file_name, download_name)]"""
    async with db() as s:
        media = Media(title=title, media_type="tv", tmdb_id=tmdb_id, status="tracking")
        s.add(media)
        await s.flush()
        mid = media.id
        for episode, file_name, download_name in rows:
            dq = DownloadQueue(
                media_id=mid, episode=episode, file_name=file_name, file_size=1024,
                share_code="sc123", stoken="stoken-x", fids='["f1"]',
                fid_tokens='["ft1"]', folder_id="folder-1",
                download_name=download_name, status="scrape",
                node_attempt=1, node_started_at=_now(), updated_at=_now(),
            )
            s.add(dq)
        await s.commit()
        return mid


async def read_dq_rows(db, media_id):
    """读取该 media 全部 download_queue 行 → [(episode, status, node_attempt, node_error)]"""
    async with db() as s:
        return [
            (r.episode, r.status, r.node_attempt, r.node_error)
            for r in (
                await s.execute(
                    select(DownloadQueue).where(DownloadQueue.media_id == media_id)
                )
            ).scalars()
        ]


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

    async def fake_advance(media_id, file_name=None):
        advanced_rec["media_id"] = media_id
        advanced_rec["file_name"] = file_name
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
    assert advanced_rec == {"media_id": 88, "file_name": None}
    assert triggered == [88]
    assert refresh_calls == [1]


def test_transfer_finished_no_advance_still_triggers_library_check(monkeypatch):
    """无 scrape 任务可推进（0 推进）→ 不触发 Emby Refresh，但仍触发 library_check 轮询加速。"""
    cli = make_client(_TOKEN, monkeypatch)
    async_session_fake = FakeSession(execute_result=FakeResult(88))
    monkeypatch.setattr(nn_mod, "async_session", lambda: async_session_fake)
    monkeypatch.setattr(nn_mod, "_advance_scrape_to_library", AsyncMock(return_value=0))
    refresh_calls = []
    monkeypatch.setattr(lc_mod, "trigger_emby_refresh", lambda: refresh_calls.append(1))
    triggered = []

    async def fake_check_library_background(media_id):
        triggered.append(media_id)

    monkeypatch.setattr(nn_mod, "_check_library_background", fake_check_library_background)

    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=media_payload())

    assert resp.status_code == 200
    assert resp.json()["advanced"] == 0
    assert refresh_calls == []
    # 无法定位/无任务 → 触发 library_check 轮询加速（不丢任务，协调者裁定 4）
    assert triggered == [88]


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


# ---------------------------------------------------------------------------
# 文件级推进（T3：webhook 载荷文件名/集号定位单行，CAS 推进）
# ---------------------------------------------------------------------------

def test_single_file_webhook_advances_only_matching_row(db, monkeypatch):
    """单文件整理完成事件只推进匹配行，其余 scrape 行保持（集号规范化定位 S01E02）。"""
    mid = run(seed_media_and_scrape(db, tmdb_id=42, rows=[
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
        ("S01E02", "测试剧.S01E02.ReEnc-1080p.mkv", None),
    ]))
    monkeypatch.setattr(nn_mod, "async_session", db)

    n = run(nn_mod._advance_scrape_to_library(mid, file_name="测试剧.S01E02.mkv"))

    assert n == 1
    rows = run(read_dq_rows(db, mid))
    states = {ep: (status, node_attempt, node_error) for ep, status, node_attempt, node_error in rows}
    # S01E02 行 → library，且节点字段重置（node_attempt=0 / node_error=None）
    assert states["S01E02"] == ("library", 0, None)
    # S01E01 行保持 scrape（未被误推进）
    assert states["S01E01"][0] == "scrape"


def test_single_file_webhook_matches_by_download_name(db, monkeypatch):
    """载荷文件名无法提取集号（电影）→ 退化为精确文件名匹配（download_name/file_name）。"""
    mid = run(seed_media_and_scrape(db, tmdb_id=43, rows=[
        ("Movie-1", "原始下载名.mkv", "某某 (2024).mkv"),
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
    ]))
    monkeypatch.setattr(nn_mod, "async_session", db)

    n = run(nn_mod._advance_scrape_to_library(mid, file_name="某某 (2024).mkv"))

    assert n == 1
    rows = run(read_dq_rows(db, mid))
    states = {ep: status for ep, status, _attempt, _err in rows}
    assert states["Movie-1"] == "library"
    assert states["S01E01"] == "scrape"


def test_webhook_cannot_locate_file_advances_nothing(db, monkeypatch):
    """载荷文件名无法定位 → 0 推进 + 仍触发 library_check 轮询（不丢任务）。"""
    mid = run(seed_media_and_scrape(db, tmdb_id=44, rows=[
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
        ("S01E02", "测试剧.S01E02.ReEnc-1080p.mkv", None),
    ]))
    monkeypatch.setattr(nn_mod, "async_session", db)

    n = run(nn_mod._advance_scrape_to_library(mid, file_name="测试剧.S05E99.mkv"))

    assert n == 0
    rows = run(read_dq_rows(db, mid))
    assert all(status == "scrape" for _ep, status, _attempt, _err in rows)


def test_advance_scrape_no_file_name_returns_zero(db, monkeypatch):
    """载荷无文件名（旧版 webhook）→ 0 推进，不触碰任何 scrape 行。"""
    mid = run(seed_media_and_scrape(db, tmdb_id=45, rows=[
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
    ]))
    monkeypatch.setattr(nn_mod, "async_session", db)

    n = run(nn_mod._advance_scrape_to_library(mid, file_name=None))

    assert n == 0
    rows = run(read_dq_rows(db, mid))
    assert rows[0][1] == "scrape"


def test_webhook_transfer_finished_cannot_locate_still_polls(db, monkeypatch):
    """端到端：载荷无法定位 → advanced=0 且 _check_library_background 被触发（轮询兜底）。"""
    cli = make_client(_TOKEN, monkeypatch)
    mid = run(seed_media_and_scrape(db, tmdb_id=46, rows=[
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
    ]))
    monkeypatch.setattr(nn_mod, "async_session", db)

    triggered = []

    async def fake_check_library_background(media_id):
        triggered.append(media_id)

    monkeypatch.setattr(nn_mod, "_check_library_background", fake_check_library_background)
    monkeypatch.setattr(nn_mod, "_advance_scrape_to_library",
                        AsyncMock(return_value=0, side_effect=None))

    payload = media_payload(tmdb_id=46)
    payload["data"]["file_name"] = "测试剧.S05E99.mkv"
    resp = cli.post(f"{_ENDPOINT}?token={_TOKEN}", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["advanced"] == 0
    assert triggered == [mid]


def test_advance_resets_node_fields(db, monkeypatch):
    """推进 scrape→library 时重置节点字段：node_attempt=0 / node_finished_at=now / node_error=None。

    预置一条处于「失败态」的 scrape 行（node_attempt=2, node_error='旧错误'），
    调用 _advance_scrape_to_library 推进后断言：status='library'、node_attempt=0、
    node_finished_at 非空、node_error is None（重试计数归零、失败诊断清空）。
    """
    mid = run(seed_media_and_scrape(db, tmdb_id=47, rows=[
        ("S01E01", "测试剧.S01E01.ReEnc-1080p.mkv", None),
    ]))

    # 把行改造成失败态（模拟多次重试失败后残留 node_attempt / node_error）
    async def _preset_failure():
        async with db() as s:
            row = (
                await s.execute(
                    select(DownloadQueue).where(DownloadQueue.media_id == mid)
                )
            ).scalars().first()
            row.node_attempt = 2
            row.node_error = "旧错误"
            row.node_finished_at = None
            await s.commit()

    run(_preset_failure())
    monkeypatch.setattr(nn_mod, "async_session", db)

    n = run(nn_mod._advance_scrape_to_library(mid, file_name="测试剧.S01E01.mkv"))

    assert n == 1

    async def _read_row():
        async with db() as s:
            return (
                await s.execute(
                    select(DownloadQueue).where(DownloadQueue.media_id == mid)
                )
            ).scalars().first()

    row = run(_read_row())
    assert row.status == "library"
    assert row.node_attempt == 0
    assert row.node_finished_at is not None
    assert row.node_error is None


# fixture：无 DB 访问路径的 client（token 校验/非 JSON 分支）
@pytest.fixture()
def dbless_client(monkeypatch):
    return make_client(_TOKEN, monkeypatch)