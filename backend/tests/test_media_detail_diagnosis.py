"""详情接口诊断 DTO 单测（media-detail-update-diagnosis P1）。

覆盖：
- diagnosis 主体：最近一条 scan_media 的 scan_detail 解析（归因 / 缺失集 / 基线）
- 三态 intervention：error / baseline_suspect / 连续 3 轮 no_candidates / processing / waiting
- next_scan_at 与 not_scheduled_reason（paused / status_not_allowed / scheduler_disabled）
- 空态：无 scan_media 记录、旧 scan_detail 无归因键 → 接口 200 且字段为空态
- merged_episodes 尝试字段：retry_count / error / last_attempt_at / node_error（仅 admin）
- 无本地行时四字段为 None（不伪造成 failed）

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向临时目录，不碰生产数据。
外部服务（Emby/TMDB/调度开关）默认 mock 为离线可用，测试内按需覆盖。
"""
import json
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_media_diagnosis_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

import itertools  # noqa: E402
from functools import partial  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

_TMDB_ID = itertools.count(5000)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _call(client: TestClient, fn, *args, **kw):
    """TestClient 的 BlockingPortal.call 不支持关键字参数 → partial 适配。"""
    return client.portal.call(partial(fn, *args, **kw))


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import InviteCode, Notification, User, WatchRequest
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        for model in (Notification, InviteCode, WatchRequest):
            await session.execute(delete(model))
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None
    return password


def _login(client: TestClient) -> dict:
    admin_password = _call(client, _recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return _auth(r.json()["access_token"])


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认离线 + 调度开关开启（真实 get_job_enabled 在空 system_config 下返回 False）。"""
    from app import scheduler as scheduler_mod
    from app.services import emby as emby_mod
    from app.services import tmdb as tmdb_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=None))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[]))
    monkeypatch.setattr(tmdb_mod, "get_episode_info", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", AsyncMock(return_value=None))
    monkeypatch.setattr(tmdb_mod, "get_tv_season_air_dates", AsyncMock(return_value={}))
    monkeypatch.setattr(scheduler_mod, "get_job_enabled", AsyncMock(return_value=True))


async def _seed_media(**kw) -> int:
    from app.database import async_session
    from app.models import Media

    fields = dict(
        title="诊断剧", media_type="tv", tmdb_id=next(_TMDB_ID), status="tracking",
    )
    fields.update(kw)
    async with async_session() as session:
        media = Media(**fields)
        session.add(media)
        await session.flush()
        await session.commit()
        return media.id


async def _seed_run(media_id: int, *, status="skipped", message="", detail=None,
                    started_at=None, task_type="scan_media") -> None:
    from app.database import async_session
    from app.models import TaskRun

    now = _now()
    async with async_session() as session:
        session.add(TaskRun(
            media_id=media_id, task_type=task_type, status=status, message=message,
            started_at=started_at or now, finished_at=now,
            scan_detail=json.dumps(detail) if detail is not None else None,
        ))
        await session.commit()


async def _seed_dq(media_id: int, episode: str, **kw) -> None:
    from app.database import async_session
    from app.models import DownloadQueue

    now = _now()
    fields = dict(
        media_id=media_id, episode=episode, file_name=f"{episode}.mkv",
        file_size=1024**3, share_code="ScAa1111", status="downloading",
        enqueued_at=now, updated_at=now,
    )
    fields.update(kw)
    async with async_session() as session:
        session.add(DownloadQueue(**fields))
        await session.commit()


async def _set_config(key: str, value: str) -> None:
    from app.database import async_session
    from app.models import SystemConfig

    async with async_session() as session:
        row = await session.get(SystemConfig, key)
        if row is None:
            session.add(SystemConfig(key=key, value=value))
        else:
            row.value = value
        await session.commit()


async def _del_config(key: str) -> None:
    from app.database import async_session
    from app.models import SystemConfig

    async with async_session() as session:
        row = await session.get(SystemConfig, key)
        if row is not None:
            await session.delete(row)
            await session.commit()


def _detail(media_id: int, h: dict) -> dict:
    with TestClient(app) as client:
        r = client.get(f"/api/media/{media_id}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()


_FULL_DETAIL = {
    "missing_total": 12,
    "missing_aired_total": 12,
    "gated_out": {"count": 2, "episodes": ["S01E13", "S01E14"]},
    "skip_reason": "no_candidates",
    "missing_items": [
        {"episode": f"S01E{i:02d}", "result": "not_found"} for i in range(1, 13)
    ],
    "baseline_detail": {
        "emby_missing_count": 12, "tmdb_aired_count": 24, "ingested_count": 12,
        "ratio": 0.5, "threshold": 0.5, "source_used": "both",
        "degraded": False, "degraded_reason": None, "season_mismatch": [],
    },
    "baseline_suspect": False,
}


# ---------------------------------------------------------------------------
# 1.1 diagnosis 主体
# ---------------------------------------------------------------------------

def test_diagnosis_body():
    """scan_detail 解析：归因文案 / 缺失集截断 / 门控计数 / 基线字段；last_task_run 不变。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, _seed_run, mid, message="未获取到资源", detail=_FULL_DETAIL)
        r = client.get(f"/api/media/{mid}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    d = body["diagnosis"]

    assert d["has_scan"] is True
    assert d["scan_status"] == "skipped"
    assert d["scan_started_at"] is not None
    assert d["scan_finished_at"] is not None
    assert d["skip_reason"] == "no_candidates"
    assert "存在已播缺失 12 集" in d["skip_reason_text"]
    assert d["missing_aired_total"] == 12
    assert len(d["missing_episodes"]) == 10
    assert d["missing_episodes"][0] == "S01E01"
    assert d["missing_more"] == 2
    assert d["gated_out_count"] == 2
    assert d["baseline_suspect"] is False
    assert d["baseline_source"] == "both"
    assert d["baseline_degraded"] is False
    assert d["baseline_detail"]["ingested_count"] == 12

    # last_task_run 契约不变（任意 task_type，5 字段）
    assert set(body["last_task_run"]) == {
        "id", "task_type", "status", "message", "started_at",
    }


# ---------------------------------------------------------------------------
# 1.2 三态 + 下次巡检
# ---------------------------------------------------------------------------

def test_intervention_error():
    """最近一轮 scan_media status=error → intervention。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, 
            _seed_run, mid, status="error", message="search 阶段失败",
            detail={"failed_phase": "search"},
        )
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["intervention"]["level"] == "intervention"
    assert d["intervention"]["label"] == "已失败需干预"
    assert "search 阶段失败" in d["intervention"]["reason"]


def test_intervention_baseline_suspect():
    """baseline_suspect=True → intervention。"""
    detail = dict(_FULL_DETAIL, baseline_suspect=True)
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, _seed_run, mid, detail=detail)
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["baseline_suspect"] is True
    assert d["intervention"]["level"] == "intervention"


def test_intervention_consecutive_no_candidates():
    """连续 3 轮 no_candidates → intervention；仅 2 轮 → waiting。"""
    with TestClient(app) as client:
        h = _login(client)
        mid3 = _call(client, _seed_media)
        mid2 = _call(client, _seed_media)
        now = _now()
        for i in range(3):
            _call(client, 
                _seed_run, mid3, detail={"skip_reason": "no_candidates"},
                started_at=now - timedelta(minutes=i),
            )
        for i in range(2):
            _call(client, 
                _seed_run, mid2, detail={"skip_reason": "no_candidates"},
                started_at=now - timedelta(minutes=i),
            )
        d3 = client.get(f"/api/media/{mid3}", headers=h).json()["diagnosis"]
        d2 = client.get(f"/api/media/{mid2}", headers=h).json()["diagnosis"]
    assert d3["intervention"]["level"] == "intervention"
    assert d2["intervention"]["level"] == "waiting"


def test_consecutive_no_candidates_threshold_from_config():
    """阈值取 system_config.scan_no_candidates_alert_rounds（与告警同源，非硬编码 3）。"""
    key = "scan_no_candidates_alert_rounds"
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        now = _now()
        _call(client, _set_config, key, "2")
        try:
            for i in range(2):
                _call(client, _seed_run, mid, detail={"skip_reason": "no_candidates"},
                      started_at=now - timedelta(minutes=i))
            d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
        finally:
            _call(client, _del_config, key)
    assert d["intervention"]["level"] == "intervention"
    assert "连续 2 轮" in d["intervention"]["reason"]


def test_consecutive_no_candidates_threshold_config_above_default():
    """阈值配成 5 时，连续 3 轮不应触发干预（仍 waiting）。"""
    key = "scan_no_candidates_alert_rounds"
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        now = _now()
        _call(client, _set_config, key, "5")
        try:
            for i in range(3):
                _call(client, _seed_run, mid, detail={"skip_reason": "no_candidates"},
                      started_at=now - timedelta(minutes=i))
            d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
        finally:
            _call(client, _del_config, key)
    assert d["intervention"]["level"] == "waiting"


def test_processing_active_download():
    """存在进行中 download_queue 行 → processing + active_episode/stage。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, _seed_run, mid, detail={"skip_reason": "no_missing"})
        _call(client, _seed_dq, mid, "S01E01", status="downloading")
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["intervention"]["level"] == "processing"
    assert d["intervention"]["label"] == "正在处理"
    assert d["active_episode"] == "S01E01"
    assert d["active_stage"] == "downloading"


def test_waiting_no_missing():
    """无缺失集且无进行中任务 → waiting。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, _seed_run, mid, detail={"skip_reason": "no_missing"})
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["intervention"]["level"] == "waiting"
    assert d["intervention"]["label"] == "等待下一轮巡检"
    assert d["active_episode"] is None
    assert d["active_stage"] is None


def test_next_scan_at_never_scanned():
    """last_scan_at 为 NULL → 立即到期（_due_filter 语义），next_scan_at ≈ now。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media, scan_interval_minutes=60)
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    next_at = datetime.fromisoformat(d["next_scan_at"]).replace(tzinfo=None)
    assert abs((next_at - _now()).total_seconds()) < 120


def test_next_scan_at_zero_interval_is_immediate():
    """scan_interval_minutes=0 是合法值（COALESCE 语义，每轮到期），不得替换为全局默认。"""
    last = _now() - timedelta(minutes=5)
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media, scan_interval_minutes=0, last_scan_at=last)
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    next_at = datetime.fromisoformat(d["next_scan_at"]).replace(tzinfo=None)
    assert abs((next_at - last).total_seconds()) < 120


def test_next_scan_at_from_last_scan():
    """有 last_scan_at → next_scan_at = last_scan_at + interval。"""
    last = _now() - timedelta(minutes=10)
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media, scan_interval_minutes=60, last_scan_at=last)
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    next_at = datetime.fromisoformat(d["next_scan_at"]).replace(tzinfo=None)
    assert abs((next_at - (last + timedelta(minutes=60))).total_seconds()) < 120


def test_not_scheduled_reason_paused():
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media, status="paused")
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["not_scheduled_reason"] == "paused"
    assert d["next_scan_at"] is None


def test_not_scheduled_reason_status_not_allowed():
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media, status="error")
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["not_scheduled_reason"] == "status_not_allowed"
    assert d["next_scan_at"] is None


def test_not_scheduled_reason_scheduler_disabled(monkeypatch):
    from app import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "get_job_enabled", AsyncMock(return_value=False))
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        d = client.get(f"/api/media/{mid}", headers=h).json()["diagnosis"]
    assert d["not_scheduled_reason"] == "scheduler_disabled"
    assert d["next_scan_at"] is None


# ---------------------------------------------------------------------------
# 1.3 空态与旧记录兼容
# ---------------------------------------------------------------------------

def test_empty_state_no_scan():
    """无 scan_media 记录 → has_scan=False，其余空态，接口 200。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        r = client.get(f"/api/media/{mid}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()["diagnosis"]
    assert d["has_scan"] is False
    for key in ("scan_started_at", "scan_finished_at", "scan_status",
                "skip_reason", "skip_reason_text", "baseline_source", "baseline_detail"):
        assert d[key] is None, key
    assert d["missing_aired_total"] == 0
    assert d["missing_episodes"] == []
    assert d["missing_more"] == 0
    assert d["gated_out_count"] == 0
    assert d["baseline_suspect"] is False
    assert d["baseline_degraded"] is False
    assert d["intervention"]["level"] == "waiting"


def test_legacy_scan_detail_without_attribution():
    """旧 scan_detail 无 skip_reason / baseline_* → 对应字段空态，不报错。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, 
            _seed_run, mid, detail={"missing_total": 0, "missing_items": []},
        )
        r = client.get(f"/api/media/{mid}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()["diagnosis"]
    assert d["has_scan"] is True
    assert d["skip_reason"] is None
    assert d["skip_reason_text"] is None
    assert d["baseline_source"] is None
    assert d["baseline_detail"] is None
    assert d["baseline_suspect"] is False


def test_non_dict_scan_detail_does_not_break():
    """scan_detail 解析为非 dict（如 JSON 数组）→ 接口 200 + 空态，不 500。"""
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        now = _now()
        for i in range(3):
            _call(client, _seed_run, mid, detail=["not-a-dict"],
                  started_at=now - timedelta(minutes=i))
        r = client.get(f"/api/media/{mid}", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()["diagnosis"]
    assert d["has_scan"] is True
    assert d["skip_reason"] is None
    assert d["intervention"]["level"] == "waiting"


# ---------------------------------------------------------------------------
# 3.1 / 3.2 集数行尝试字段
# ---------------------------------------------------------------------------

def _mock_tmdb_episodes(monkeypatch, episodes):
    from app.services import tmdb as tmdb_mod

    monkeypatch.setattr(tmdb_mod, "get_episode_info", AsyncMock(return_value=episodes))


def test_episode_attempt_fields(monkeypatch):
    """merged_episodes 从 download_queue 行补齐 retry_count/error/last_attempt_at/node_error。"""
    _mock_tmdb_episodes(monkeypatch, [
        {"season": 1, "episode": 1, "name": "E01", "air_date": "2020-01-01"},
        {"season": 1, "episode": 2, "name": "E02", "air_date": "2020-01-02"},
    ])
    started = _now() - timedelta(hours=2)
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, 
            _seed_dq, mid, "S01E01", status="failed", retry_count=2,
            error="下载失败", node_started_at=started, node_error="节点超时",
        )
        body = client.get(f"/api/media/{mid}", headers=h).json()
    by_ep = {e["episode"]: e for e in body["episode_state"]}
    assert by_ep["S01E01"]["retry_count"] == 2
    assert by_ep["S01E01"]["error"] == "下载失败"
    assert by_ep["S01E01"]["node_error"] == "节点超时"
    assert datetime.fromisoformat(by_ep["S01E01"]["last_attempt_at"]).replace(
        tzinfo=None) == started


def test_episode_empty_state(monkeypatch):
    """无本地行 → 四字段为 None（不得伪造 failed）；已入库行的 error 仍下发。"""
    _mock_tmdb_episodes(monkeypatch, [
        {"season": 1, "episode": 1, "name": "E01", "air_date": "2020-01-01"},
        {"season": 1, "episode": 2, "name": "E02", "air_date": "2020-01-02"},
    ])
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(
        return_value=[{"code": "S01E02"}]))
    with TestClient(app) as client:
        h = _login(client)
        mid = _call(client, _seed_media)
        _call(client, 
            _seed_dq, mid, "S01E02", status="done", error="历史失败原因",
        )
        body = client.get(f"/api/media/{mid}", headers=h).json()
    by_ep = {e["episode"]: e for e in body["episode_state"]}
    # 无本地行：四字段均 None，state 不是 failed
    assert by_ep["S01E01"]["retry_count"] is None
    assert by_ep["S01E01"]["error"] is None
    assert by_ep["S01E01"]["last_attempt_at"] is None
    assert by_ep["S01E01"]["node_error"] is None
    assert by_ep["S01E01"]["state"] != "failed"
    # 已入库行：error 仍下发（前端决定不渲染）
    assert by_ep["S01E02"]["state"] == "in_library"
    assert by_ep["S01E02"]["error"] == "历史失败原因"


def test_episode_node_error_admin_only(monkeypatch):
    """node_error 仅 admin 下发；guest 不返回该键。"""
    _mock_tmdb_episodes(monkeypatch, [
        {"season": 1, "episode": 1, "name": "E01", "air_date": "2020-01-01"},
    ])
    with TestClient(app) as client:
        admin = _login(client)
        mid = _call(client, _seed_media)
        _call(client, 
            _seed_dq, mid, "S01E01", status="failed", retry_count=1,
            error="下载失败", node_error="节点超时",
        )
        # guest 注册 + 登录
        r = client.post("/api/admin/invites", json={"count": 1}, headers=admin)
        invite = r.json()["codes"][0]
        client.post("/api/auth/register", json={
            "username": "guestdiag", "password": "pass1234", "invite_code": invite,
        })
        guest = _auth(client.post("/api/auth/login", json={
            "username": "guestdiag", "password": "pass1234",
        }).json()["access_token"])
        admin_row = client.get(f"/api/media/{mid}", headers=admin).json()["episode_state"][0]
        guest_row = client.get(f"/api/media/{mid}", headers=guest).json()["episode_state"][0]
    assert admin_row["node_error"] == "节点超时"
    assert "node_error" not in guest_row
    # 非敏感尝试字段 guest 仍可见
    assert guest_row["retry_count"] == 1
    assert guest_row["error"] == "下载失败"
