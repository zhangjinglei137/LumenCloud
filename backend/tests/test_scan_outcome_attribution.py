"""scan-outcome-attribution P1 单测：归因字段（1.1）/ 结论文案（1.2）/ skipped 兼容（1.3）/ 异常兜底（3.2）。

- 1.1：各结束分支的 `scan_detail` 均非空，并带 `skip_reason` / `gated_out` /
  `missing_aired_total`；覆盖 no_missing / not_aired / gated_out / no_candidates /
  skip_enqueue 五类 + paused/error / baseline_unavailable / airdate_unavailable。
- 1.2：`_skip_message` 9 类 reason 文案互不相同、含缺失集数与影视名，
  「因下载中未入队」不得表述为「已有任务跳过」。
- 1.3：`skipped` 语义收窄的兼容性——运行日志解析对 `scan_detail=None` 与新增键均容错。
- 3.2：`_scan_one` 异常逃逸时把当前 task_run 收敛为 error 且不吞异常。

风格对齐 test_scan_run_phases.py：mock 外部依赖，独立 in-memory SQLite。
"""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media, Notification, SystemConfig, TaskQueue, TaskRun


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_scan_alert_cooldown():
    """每个测试前重置 scan 告警冷却表与 PushPlus 降级节流表（进程级共享状态）。"""
    from app.services import notifier as notifier_mod
    from app.tasks import scan as scan_mod

    scan_table = getattr(scan_mod, "_scan_alert_cooldown", None)
    if scan_table:
        scan_table.clear()
    notifier_mod._pushplus_alert_cooldown.clear()
    yield
    scan_table = getattr(scan_mod, "_scan_alert_cooldown", None)
    if scan_table:
        scan_table.clear()
    notifier_mod._pushplus_alert_cooldown.clear()


class _RecordingNotifier:
    """记录 notify 事件的 notifier 桩。"""

    def __init__(self) -> None:
        self.events: list = []

    async def notify(self, event) -> None:
        self.events.append(event)


class _FailingPushPlusClient:
    """send 必抛异常的 PushPlus 客户端桩。"""

    async def send(self, title: str, content: str, template: str = "txt") -> dict:
        raise RuntimeError("pushplus 通道故障")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


async def _seed_media(db, *, title="测试剧", media_type="tv", status="tracking", tmdb_id=42):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=tmdb_id, status=status)
        s.add(media)
        await s.commit()
        return media.id


def _patch_env(monkeypatch, db, *, missing, air_date="2020-01-01", candidates=None,
               files=None, all_eps=None):
    """scan 外部依赖全 mock（emby 基线 / TMDB 播出日期 / 搜索 / 分享 / 大小 / transfer）。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=missing))

    async def _fake_air_dates(tmdb_id, season_number):
        out = {}
        for c in (missing or []):
            code = c if isinstance(c, str) else (c or {}).get("code")
            if not code or scan_mod._season_of_key(code) != season_number:
                continue
            ep = scan_mod._parse_episode_number(code)
            if ep is not None:
                out[ep] = air_date
        return out

    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates", _fake_air_dates)
    if all_eps is not None:
        monkeypatch.setattr(scan_mod.tmdb, "get_tv_all_episodes",
                            AsyncMock(return_value=all_eps))
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=candidates or []))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                               "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": files or []}),
    )
    monkeypatch.setattr(scan_mod, "_read_size_limits",
                        AsyncMock(return_value=(0.0, 100.0, 0.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_media_total_episodes", AsyncMock(return_value=8))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


def _file(name: str, size: int = 1024) -> dict:
    return {"fileName": name, "fileId": f"f_{name}", "fileIdToken": "ft",
            "isFolder": False, "size": size}


def _detail(tr) -> dict:
    return json.loads(tr.scan_detail)


async def _drain_scan_background(scan_mod) -> None:
    """等待 scan 模块登记的后台任务结束（测试内保持同一事件循环）。"""
    for _ in range(10):
        pending = [t for t in scan_mod._background if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _read_runs(db, mid):
    async def _case():
        async with db() as s:
            return (
                await s.execute(
                    select(TaskRun).where(TaskRun.media_id == mid).order_by(TaskRun.id.asc())
                )
            ).scalars().all()

    return run(_case())


# ---------------------------------------------------------------------------
# 1.1 scan_detail 骨架与各结束分支归因
# ---------------------------------------------------------------------------

def test_skeleton_has_attribution_keys():
    """骨架含 skip_reason / gated_out / missing_aired_total / trigger_source。"""
    from app.tasks.scan import _result_detail_skeleton

    sk = _result_detail_skeleton()
    assert sk["skip_reason"] is None
    assert sk["gated_out"] == {"count": 0, "episodes": []}
    assert sk["missing_aired_total"] == 0
    assert "trigger_source" in sk


def test_skip_reason_status_paused_and_error(db, monkeypatch):
    """paused / error 状态预检：scan_detail 非空 + 对应 skip_reason。"""
    scan_mod = _patch_env(monkeypatch, db, missing=[])

    mid_paused = run(_seed_media(db, status="paused"))
    run(scan_mod._scan_one(mid_paused))
    tr = _read_runs(db, mid_paused)[0]
    assert tr.status == "skipped"
    assert _detail(tr)["skip_reason"] == "status_paused"
    assert "media.status=paused，跳过巡检" in tr.message

    mid_error = run(_seed_media(db, status="error", title="测试剧2", tmdb_id=43))
    run(scan_mod._scan_one(mid_error))
    tr2 = _read_runs(db, mid_error)[0]
    assert tr2.status == "skipped"
    assert _detail(tr2)["skip_reason"] == "status_error"


def test_skip_reason_baseline_unavailable(db, monkeypatch):
    """Emby 未收录且强防重（missing=None）→ baseline_unavailable + 骨架非空。"""
    scan_mod = _patch_env(monkeypatch, db, missing=None)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    assert tr.status == "skipped"
    assert _detail(tr)["skip_reason"] == "baseline_unavailable"
    assert "防重基线强制" in tr.message and "scan_baseline_required=True" in tr.message


def test_skip_reason_airdate_unavailable(db, monkeypatch):
    """TMDB 某季取数失败且门控后无保留项 → airdate_unavailable + 季号。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value=None))
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    assert tr.status == "skipped"
    detail = _detail(tr)
    assert detail["skip_reason"] == "airdate_unavailable"
    assert "TMDB 播出日期取数失败" in tr.message and "季 [1]" in tr.message


def test_skip_reason_gated_out_records_dropped_episodes(db, monkeypatch):
    """缺失集全部未播出 → gated_out + 被剔除集数与集号，且不搜索不入队。"""
    scan_mod = _patch_env(monkeypatch, db, air_date="2999-01-01",
                          missing=[{"code": "S01E01", "premiere_date": None},
                                   {"code": "S01E02", "premiere_date": None}])
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert tr.status == "skipped"
    assert detail["skip_reason"] == "gated_out"
    assert detail["gated_out"] == {"count": 2, "episodes": ["S01E01", "S01E02"]}
    assert detail["missing_aired_total"] == 0
    scan_mod._search_and_rank.assert_not_awaited()


def test_skip_reason_not_aired_full_mode(db, monkeypatch):
    """tv 全量兜底：TMDB 全集均未播出 → not_aired。"""
    scan_mod = _patch_env(monkeypatch, db, missing=[None],
                          all_eps=[{"season": 1, "episode": 1, "air_date": "2999-01-01"}])
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    assert tr.status == "skipped"
    assert _detail(tr)["skip_reason"] == "not_aired"
    assert "TMDB 无已播出集（未开播），本轮跳过" in tr.message
    scan_mod._search_and_rank.assert_not_awaited()


def test_skip_reason_no_missing(db, monkeypatch):
    """无缺失集 → no_missing（而非 None），且 missing_aired_total=0。"""
    scan_mod = _patch_env(monkeypatch, db, missing=[])
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert detail["skip_reason"] == "no_missing"
    assert detail["missing_aired_total"] == 0
    assert "无遗漏集（Emby 基线已覆盖），跳过" in tr.message


def test_skip_reason_no_candidates_with_aired_missing(db, monkeypatch):
    """有已播缺失集但搜索无候选 → no_candidates + missing_aired_total。"""
    scan_mod = _patch_env(monkeypatch, db, candidates=[],
                          missing=[{"code": "S01E01", "premiere_date": None}])
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert tr.status == "skipped"
    assert detail["skip_reason"] == "no_candidates"
    assert detail["missing_aired_total"] == 1
    assert "未找到缺失集 S01E01 的资源" in tr.message
    assert "未获取到任何资源" in tr.message


def test_skip_reason_skip_enqueue_downloading(db, monkeypatch):
    """downloading 状态检出已播缺失集 → skip_enqueue，且不误述「已有任务跳过」。"""
    scan_mod = _patch_env(
        monkeypatch, db,
        missing=[{"code": "S01E01", "premiere_date": None}],
        candidates=[{"title": "测试剧 S01", "share_code": "abc123"}],
        files=[_file("S01E01.mkv")],
    )
    mid = run(_seed_media(db, status="downloading"))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert tr.status == "skipped"
    assert detail["skip_reason"] == "skip_enqueue"
    assert detail["missing_aired_total"] == 1
    assert "因正在下载未入队 1 集" in tr.message
    assert "已有任务跳过" not in tr.message

    async def _tq():
        async with db() as s:
            return (await s.execute(select(TaskQueue))).scalars().all()

    assert run(_tq()) == []


def test_success_run_records_aired_total_and_no_skip_reason(db, monkeypatch):
    """正常入队：skip_reason 为 None，missing_aired_total 为已播缺失集数。"""
    scan_mod = _patch_env(
        monkeypatch, db,
        missing=[{"code": "S01E01", "premiere_date": None},
                 {"code": "S01E02", "premiere_date": None}],
        candidates=[{"title": "测试剧 S01", "share_code": "abc123"}],
        files=[_file("S01E01.mkv"), _file("S01E02.mkv")],
    )
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert tr.status == "success"
    assert detail["skip_reason"] is None
    assert detail["missing_aired_total"] == 2
    assert detail["gated_out"] == {"count": 0, "episodes": []}


def _no_candidates_case(db, monkeypatch, *, files, title="测试剧"):
    """有已播缺失集 + 1 个候选分享：跑一轮，返回 (run, detail)。"""
    scan_mod = _patch_env(
        monkeypatch, db,
        missing=[{"code": "S01E01", "premiere_date": None}],
        candidates=[{"title": "测试剧 S01", "share_code": "abc123"}],
        files=files,
    )
    mid = run(_seed_media(db, title=title))
    run(scan_mod._scan_one(mid))
    tr = _read_runs(db, mid)[0]
    return tr, _detail(tr)


def test_no_candidates_not_set_when_size_filtered(db, monkeypatch):
    """有资源但被大小过滤 → 不是「搜不到资源」，skip_reason 保持 None。"""
    tr, detail = _no_candidates_case(
        db, monkeypatch, files=[_file("S01E01.mkv", 500 * 1024 ** 3)],
    )
    assert detail["size_filtered"] == 1
    assert detail["skip_reason"] is None
    assert "大小不符" in tr.message


def test_no_candidates_not_set_when_unmatched(db, monkeypatch):
    """有候选文件但集号未匹配 → skip_reason 保持 None（计数文案已表达原因）。"""
    tr, detail = _no_candidates_case(db, monkeypatch, files=[_file("S03E99.mkv")])
    assert detail["unmatched"] == 1
    assert detail["skip_reason"] is None


def test_no_candidates_not_set_when_only_non_video(db, monkeypatch):
    """只有非视频文件 → skip_reason 保持 None。"""
    tr, detail = _no_candidates_case(db, monkeypatch, files=[_file("Cover.jpg")])
    assert detail["non_video"] == 1
    assert detail["skip_reason"] is None


def test_no_candidates_set_for_tv_full_mode_zero_candidates(db, monkeypatch):
    """tv 未收录软全量 + 搜索零候选：已播集数来自 2c 门控，必须归因 no_candidates。"""
    scan_mod = _patch_env(
        monkeypatch, db, missing=[None], candidates=[],
        all_eps=[{"season": 1, "episode": 1, "air_date": "2020-01-01"},
                 {"season": 1, "episode": 2, "air_date": "2020-01-02"}],
    )
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert tr.status == "skipped"
    assert detail["skip_reason"] == "no_candidates"
    assert detail["missing_aired_total"] == 2
    assert "存在已播缺失 2 集" in tr.message


def test_no_candidates_set_for_movie_full_mode_zero_candidates(db, monkeypatch):
    """真电影整部缺失 + 搜索零候选：按 1 个缺失实体归因 no_candidates。"""
    scan_mod = _patch_env(monkeypatch, db, missing=[None], candidates=[])
    mid = run(_seed_media(db, media_type="movie", title="大话西游"))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert detail["skip_reason"] == "no_candidates"
    assert detail["missing_aired_total"] == 1
    assert "存在已播缺失 1 集" in tr.message


def test_no_candidates_not_set_when_unrelated_filtered(db, monkeypatch):
    """A2 文件级校验拒绝（unrelated_filtered）说明找到过资源 → 不置 no_candidates。"""
    scan_mod = _patch_env(
        monkeypatch, db, missing=[None],
        candidates=[{"title": "少帅 全量", "share_code": "abc123"}],
        files=[_file("9.mp4")],
        all_eps=[{"season": 1, "episode": 1, "air_date": "2020-01-01"}],
    )
    mid = run(_seed_media(db, title="少帅"))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert detail["unrelated_filtered"] == 1
    assert detail["skip_reason"] is None


# ---------------------------------------------------------------------------
# 1.2 _skip_message：9 类 reason 文案
# ---------------------------------------------------------------------------

REASONS = (
    "status_paused", "status_error", "baseline_unavailable", "airdate_unavailable",
    "no_missing", "not_aired", "gated_out", "no_candidates", "skip_enqueue",
)


def test_skip_message_nine_reasons_are_distinct():
    """9 类 reason 文案互不相同；「无遗漏集」只用于真正的 no_missing。"""
    from app.tasks.scan import _skip_message

    msgs = {
        r: _skip_message(
            r, title="测试剧", detail="季 [1]", missing_aired_total=3,
            missing_episodes=["S01E01", "S01E02", "S01E03"],
            gated_out_episodes=["S01E01", "S01E02"], not_enqueued=2,
        )
        for r in REASONS
    }
    assert len(set(msgs.values())) == len(REASONS)
    for reason, msg in msgs.items():
        assert msg.strip()
        if reason != "no_missing":
            assert "无遗漏集" not in msg, reason


def test_skip_message_carries_counts_and_title():
    """文案含缺失集数与影视名；下载中未入队不得写成「已有任务跳过」。"""
    from app.tasks.scan import _skip_message

    no_cand = _skip_message("no_candidates", title="凡人修仙传", missing_aired_total=3,
                            missing_episodes=["S01E01", "S01E02", "S01E03"])
    assert "凡人修仙传" in no_cand and "3 集" in no_cand

    downloading = _skip_message("skip_enqueue", title="凡人修仙传",
                                missing_aired_total=3, not_enqueued=2)
    assert "因正在下载未入队 2 集" in downloading
    assert "已有任务跳过" not in downloading


def test_skip_message_unknown_reason_raises():
    """未知 reason 直接报错，防拼写错误静默产出空文案。"""
    from app.tasks.scan import _skip_message

    with pytest.raises(ValueError):
        _skip_message("bogus_reason")


def test_skip_message_truncates_long_episode_lists():
    """集号超过 10 个时截断为前 10 个 + 「等 N 集」，message 长度有界。"""
    from app.tasks.scan import _skip_message

    eps = [f"S01E{i:02d}" for i in range(1, 13)]  # 12 集
    for reason in ("gated_out", "no_candidates"):
        msg = _skip_message(reason, title="斗破苍穹", missing_aired_total=len(eps),
                            missing_episodes=eps, gated_out_episodes=eps)
        assert "S01E10" in msg
        assert "S01E11" not in msg and "S01E12" not in msg
        assert "等 12 集" in msg
        assert len(msg) < 200, msg


def test_gated_out_message_bounded_but_detail_keeps_all(db, monkeypatch):
    """门控剔除 12 集：message 截断，scan_detail.gated_out.episodes 保留完整列表。"""
    eps = [{"code": f"S01E{i:02d}", "premiere_date": None} for i in range(1, 13)]
    scan_mod = _patch_env(monkeypatch, db, air_date="2999-01-01", missing=eps)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    detail = _detail(tr)
    assert detail["gated_out"]["count"] == 12
    assert len(detail["gated_out"]["episodes"]) == 12
    assert "等 12 集" in tr.message
    assert "S01E11" not in tr.message
    assert len(tr.message) < 200, tr.message


# ---------------------------------------------------------------------------
# 1.3 skipped 语义收窄的兼容性
# ---------------------------------------------------------------------------

def test_logs_json_detail_tolerates_none_and_new_keys():
    """运行日志解析：scan_detail=None 仍返回 None；新归因键可正常解析。"""
    from app.routers.logs import _json_or_none

    assert _json_or_none(None) is None
    parsed = _json_or_none(json.dumps({
        "skip_reason": "no_missing",
        "gated_out": {"count": 0, "episodes": []},
        "missing_aired_total": 0,
    }))
    assert parsed["skip_reason"] == "no_missing"


def test_paused_run_scan_detail_readable_by_logs(db, monkeypatch):
    """短路分支现在落骨架：日志侧照常解析为 dict（不再依赖 scan_detail is None）。"""
    from app.routers.logs import _json_or_none
    from app.tasks import scan as scan_mod

    _patch_env(monkeypatch, db, missing=[])
    mid = run(_seed_media(db, status="paused"))
    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    parsed = _json_or_none(tr.scan_detail)
    assert isinstance(parsed, dict)
    assert parsed["skip_reason"] == "status_paused"


# ---------------------------------------------------------------------------
# 3.2 _scan_one 异常兜底
# ---------------------------------------------------------------------------

def test_scan_one_finally_converges_run_to_error(db, monkeypatch):
    """异常逃逸：当前 task_run 收敛为 error（含原因），异常继续上抛，无 running 残留。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_filter_aired",
                        AsyncMock(side_effect=RuntimeError("gate boom")))
    mid = run(_seed_media(db))

    with pytest.raises(RuntimeError, match="gate boom"):
        run(scan_mod._scan_one(mid))

    runs = _read_runs(db, mid)
    assert len(runs) == 1
    assert runs[0].status == "error"
    assert "gate boom" in runs[0].message
    assert runs[0].finished_at is not None

    async def _running():
        async with db() as s:
            return (
                await s.execute(select(TaskRun).where(TaskRun.status == "running"))
            ).scalars().all()

    assert run(_running()) == []


# ---------------------------------------------------------------------------
# 2.1 巡检 error 告警
# ---------------------------------------------------------------------------

def test_error_alert_contains_title_phase_and_reason(db, monkeypatch):
    """巡检以 error 结束 → 发一条 flow_error，正文含影视名 / 失败阶段 / 原因。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db, title="凡人修仙传"))

    run(scan_mod._scan_one(mid))

    assert len(recorder.events) == 1
    event = recorder.events[0]
    assert event.event_type == "flow_error"
    assert "凡人修仙传" in event.body
    assert "search" in event.body
    assert "search boom" in event.body
    assert event.extra.get("media_id") == mid
    # 落库不受告警影响
    assert _read_runs(db, mid)[0].status == "error"


def test_error_alert_failure_does_not_affect_run_persistence(db, monkeypatch):
    """通知发送失败只记日志：巡检结果仍正常落库为 error。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))

    class _BoomNotifier:
        async def notify(self, event):
            raise RuntimeError("notify boom")

    monkeypatch.setattr(scan_mod, "notifier", _BoomNotifier())
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    assert tr.status == "error"
    assert "search boom" in tr.message


def test_error_alert_cooldown_suppresses_same_phase(db, monkeypatch):
    """同一影视同一失败原因在 24h 冷却窗口内只告警一次。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))
    run(scan_mod._scan_one(mid))

    assert len(recorder.events) == 1
    assert len(_read_runs(db, mid)) == 2


# ---------------------------------------------------------------------------
# 2.2 连续 N 轮 no_candidates 告警
# ---------------------------------------------------------------------------

def _seed_config(db, key, value):
    async def _case():
        async with db() as s:
            s.add(SystemConfig(key=key, value=value))
            await s.commit()

    run(_case())


def test_no_candidates_alert_once_after_three_rounds(db, monkeypatch):
    """连续 3 轮 no_candidates 且 missing_aired_total>0 → 只告警一次；第 4 轮不重复。"""
    scan_mod = _patch_env(
        monkeypatch, db, candidates=[],
        missing=[{"code": "S01E01", "premiere_date": None},
                 {"code": "S01E02", "premiere_date": None},
                 {"code": "S01E03", "premiere_date": None}],
    )
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db, title="凡人修仙传"))

    for _ in range(3):
        run(scan_mod._scan_one(mid))
    assert len(recorder.events) == 1
    body = recorder.events[0].body
    assert "凡人修仙传" in body and "3 集" in body and "3 轮" in body

    run(scan_mod._scan_one(mid))  # 第 4 轮仍在冷却窗口内
    assert len(recorder.events) == 1


def test_no_candidates_alert_rounds_from_system_config(db, monkeypatch):
    """告警轮次阈值读 system_config（此例 2 轮）。"""
    _seed_config(db, "scan_no_candidates_alert_rounds", "2")
    scan_mod = _patch_env(
        monkeypatch, db, candidates=[],
        missing=[{"code": "S01E01", "premiere_date": None}],
    )
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))
    assert recorder.events == []
    run(scan_mod._scan_one(mid))
    assert len(recorder.events) == 1


def test_no_candidates_alert_rearmed_after_missing_declines(db, monkeypatch):
    """缺失集数量下降时清除冷却键，之后可再次告警。"""
    scan_mod = _patch_env(
        monkeypatch, db, candidates=[],
        missing=[{"code": "S01E01", "premiere_date": None},
                 {"code": "S01E02", "premiere_date": None},
                 {"code": "S01E03", "premiere_date": None}],
    )
    recorder = _RecordingNotifier()
    monkeypatch.setattr(scan_mod, "notifier", recorder)
    mid = run(_seed_media(db))

    for _ in range(3):
        run(scan_mod._scan_one(mid))
    assert len(recorder.events) == 1

    # 缺失集 3 → 2：清除冷却，本轮不再重复告警
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", AsyncMock(return_value=[
        {"code": "S01E01", "premiere_date": None},
        {"code": "S01E02", "premiere_date": None},
    ]))
    run(scan_mod._scan_one(mid))
    assert len(recorder.events) == 1
    assert "1:no_candidates" not in scan_mod._scan_alert_cooldown

    # 冷却已清 → 下一轮重新满足连续 3 轮即可再次告警
    run(scan_mod._scan_one(mid))
    assert len(recorder.events) == 2


# ---------------------------------------------------------------------------
# 2.3 PushPlus 降级（巡检告警）
# ---------------------------------------------------------------------------

def test_scan_alert_written_inapp_when_pushplus_unconfigured(db, monkeypatch):
    """PushPlus 未配置：链上的 InApp 仍落一条巡检告警。"""
    from app.services.notifier import InAppNotifier, NotifierChain, PushPlusNotifier

    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))
    push = PushPlusNotifier()
    push._client = None
    push._refresh_client = lambda: None
    monkeypatch.setattr(scan_mod, "notifier",
                        NotifierChain([InAppNotifier(session_factory=db), push]))
    mid = run(_seed_media(db, title="凡人修仙传"))

    run(scan_mod._scan_one(mid))

    async def _rows():
        async with db() as s:
            return (await s.execute(select(Notification))).scalars().all()

    rows = run(_rows())
    assert len(rows) == 1
    assert rows[0].event_type == "flow_error"
    assert "凡人修仙传" in rows[0].body


def test_scan_alert_pushplus_failure_falls_back_to_inapp(db, monkeypatch):
    """PushPlus 推送失败：巡检告警仍落站内，并补发一条推送失败降级通知。"""
    from app.services.notifier import InAppNotifier, NotifierChain, PushPlusNotifier

    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))
    inapp = InAppNotifier(session_factory=db)
    push = PushPlusNotifier()
    push._client = _FailingPushPlusClient()
    push._refresh_client = lambda: None
    push._fallback = inapp
    monkeypatch.setattr(scan_mod, "notifier", NotifierChain([inapp, push]))
    mid = run(_seed_media(db, title="凡人修仙传"))

    run(scan_mod._scan_one(mid))

    async def _rows():
        async with db() as s:
            return (await s.execute(select(Notification).order_by(Notification.id))).scalars().all()

    rows = run(_rows())
    titles = [r.title for r in rows]
    assert any("巡检失败" in t for t in titles)
    assert any("PushPlus 推送失败" in t for t in titles)
    assert any("凡人修仙传" in r.body for r in rows)


# ---------------------------------------------------------------------------
# 4.1 手动触发同步建 manual 记录并复用
# ---------------------------------------------------------------------------

def test_manual_trigger_creates_single_manual_run(db, monkeypatch):
    """trigger_scan_background 受理即建 manual run，后台复用同一条（不产生两条）。"""
    scan_mod = _patch_env(monkeypatch, db, candidates=[],
                          missing=[{"code": "S01E01", "premiere_date": None}])
    mid = run(_seed_media(db))

    async def _case():
        task = scan_mod.trigger_scan_background(mid, manual=True)
        run_id = await task
        await _drain_scan_background(scan_mod)
        return run_id

    run_id = run(_case())

    runs = _read_runs(db, mid)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status in ("skipped", "success")
    detail = _detail(runs[0])
    assert detail["trigger_source"] == "manual"
    assert detail["skip_reason"] == "no_candidates"


def test_auto_scan_records_auto_trigger_source(db, monkeypatch):
    """定时路径（无 run_id）自建记录，trigger_source=auto。"""
    scan_mod = _patch_env(monkeypatch, db, missing=[])
    mid = run(_seed_media(db))

    run(scan_mod._scan_one(mid))

    tr = _read_runs(db, mid)[0]
    assert _detail(tr)["trigger_source"] == "auto"


def test_media_scan_route_returns_run_id_and_accepted(db, monkeypatch):
    """POST /api/media/{id}/scan 返回受理结果与 run id。"""
    import types

    from app.routers.media import scan_media_route

    scan_mod = _patch_env(monkeypatch, db, missing=[])
    mid = run(_seed_media(db))

    async def _case():
        res = await scan_media_route(mid, admin=types.SimpleNamespace(id=1))
        await _drain_scan_background(scan_mod)
        return res

    res = run(_case())

    assert res["ok"] is True
    assert res["accepted"] is True
    assert res["run_id"] == _read_runs(db, mid)[0].id


# ---------------------------------------------------------------------------
# 4.2 队列 retry 记录 manual 来源
# ---------------------------------------------------------------------------

def test_queue_retry_records_manual_trigger_source(db, monkeypatch):
    """retry 成功后写一条 trigger_source=manual 的运行记录。"""
    from app.routers import queue as queue_mod

    monkeypatch.setattr(queue_mod, "_trigger_consume", AsyncMock(return_value=None))

    async def _seed():
        from app.models import DownloadQueue

        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", status="failed",
                retry_count=3, node_attempt=3, file_name="ep.mkv", file_size=1024,
                share_code="sc", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", updated_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return media.id, dq.id

    mid, dq_id = run(_seed())

    async def _retry():
        async with db() as s:
            return await queue_mod.retry_task(
                task_id=dq_id, admin=type("A", (), {"id": 1})(), session=s,
            )

    assert run(_retry()) == {"ok": True}

    runs = _read_runs(db, mid)
    assert len(runs) == 1
    assert runs[0].task_type == "queue_retry"
    assert _detail(runs[0])["trigger_source"] == "manual"


# ---------------------------------------------------------------------------
# 4.3 后台异常时 manual 记录收敛为 error
# ---------------------------------------------------------------------------

def test_manual_background_exception_converges_to_error(db, monkeypatch):
    """manual 记录在后台执行异常时收敛为 error 并保留原因，无 running 残留。"""
    scan_mod = _patch_env(monkeypatch, db,
                          missing=[{"code": "S01E01", "premiere_date": None}])
    monkeypatch.setattr(scan_mod, "_filter_aired",
                        AsyncMock(side_effect=RuntimeError("gate boom")))
    monkeypatch.setattr(scan_mod, "notifier", _RecordingNotifier())
    mid = run(_seed_media(db))

    async def _case():
        task = scan_mod.trigger_scan_background(mid, manual=True)
        run_id = await task
        await _drain_scan_background(scan_mod)
        return run_id

    run_id = run(_case())

    runs = _read_runs(db, mid)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status == "error"
    assert "gate boom" in runs[0].message
    assert _detail(runs[0])["trigger_source"] == "manual"
    assert runs[0].finished_at is not None


# ---------------------------------------------------------------------------
# 审查补充：retry 留痕 commit 失败不反噬重试；受理失败明确反馈
# ---------------------------------------------------------------------------

def test_queue_retry_succeeds_when_retry_record_commit_fails(db, monkeypatch):
    """留痕 commit 抛异常 → rollback 后重试仍返回成功（不出现 500 / PendingRollback）。"""
    from app.models import DownloadQueue
    from app.routers import queue as queue_mod

    monkeypatch.setattr(queue_mod, "_trigger_consume", AsyncMock(return_value=None))

    async def _seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", status="failed",
                retry_count=3, node_attempt=3, file_name="ep.mkv", file_size=1024,
                share_code="sc", stoken="st", receive_code="rc", fids="[]",
                fid_tokens="[]", folder_id="fd", updated_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return media.id, dq.id

    mid, dq_id = run(_seed())

    async def _retry():
        async with db() as s:
            real_commit = s.commit
            calls = {"n": 0}

            async def _commit():
                calls["n"] += 1
                if calls["n"] == 2:  # 第 1 次是重试本身，第 2 次是留痕
                    raise RuntimeError("record commit boom")
                await real_commit()

            monkeypatch.setattr(s, "commit", _commit)
            return await queue_mod.retry_task(
                task_id=dq_id, admin=type("A", (), {"id": 1})(), session=s,
            )

    assert run(_retry()) == {"ok": True}

    async def _read_dq():
        async with db() as s:
            return await s.get(DownloadQueue, dq_id)

    assert run(_read_dq()).status == "pending"
    assert _read_runs(db, mid) == []  # 留痕已随 rollback 撤销


def test_media_scan_route_not_accepted_when_scan_module_unavailable(monkeypatch):
    """巡检模块导入失败（提前 return 分支）也必须返回 accepted=False。"""
    import types

    from app.routers.media import scan_media_route
    from app.tasks import scan as scan_mod

    monkeypatch.delattr(scan_mod, "trigger_scan_background")

    res = run(scan_media_route(1, admin=types.SimpleNamespace(id=1)))

    assert res["ok"] is True
    assert res["accepted"] is False
    assert res["run_id"] is None
    assert res["task_run_id"] is None


def test_media_scan_route_not_accepted_when_trigger_is_sync_stub(monkeypatch):
    """旧同步桩（非 awaitable）无法取得 run id → accepted=False。"""
    import types

    from app.routers.media import scan_media_route
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "trigger_scan_background", lambda *a, **kw: None)

    res = run(scan_media_route(1, admin=types.SimpleNamespace(id=1)))

    assert res["ok"] is True
    assert res["accepted"] is False
    assert res["run_id"] is None
    assert res["task_run_id"] is None


def test_media_scan_route_reports_not_accepted_when_media_missing(db, monkeypatch):
    """media 不存在 → accepted=False / run_id=None，且不建 running 记录、不启动巡检。"""
    import types

    from app.routers.media import scan_media_route

    scan_mod = _patch_env(monkeypatch, db, missing=[])
    scan_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(scan_mod, "scan_media", scan_mock)

    res = run(scan_media_route(9999, admin=types.SimpleNamespace(id=1)))

    assert res["accepted"] is False
    assert res["run_id"] is None
    assert res["task_run_id"] is None
    assert _read_runs(db, 9999) == []
    scan_mock.assert_not_awaited()

    async def _running():
        async with db() as s:
            return (
                await s.execute(select(TaskRun).where(TaskRun.status == "running"))
            ).scalars().all()

    assert run(_running()) == []


# ---------------------------------------------------------------------------
# 5.1 集成：完整巡检链路 + 连续 3 轮告警 + 运行日志读回
# ---------------------------------------------------------------------------

def test_integration_no_candidates_alert_and_logs_readable(db, monkeypatch):
    """有已播缺集 + 搜索无候选：走 scan_media 完整链路，3 轮告警一次，日志可读。

    只 mock 外部服务（Emby 基线 / TMDB 播出日期 / cloudSaver 搜索与分享 / 大小上下限）；
    DB 会话、scan_media → _scan_one → _scan_one_impl、NotifierChain/InAppNotifier、
    routers.logs.list_logs 均为真实实现。
    """
    import types

    from app.routers.logs import list_logs
    from app.services.notifier import InAppNotifier, NotifierChain
    from app.tasks import scan as scan_mod

    scan_mod = _patch_env(
        monkeypatch, db, candidates=[],
        missing=[{"code": "S01E01", "premiere_date": None},
                 {"code": "S01E02", "premiere_date": None},
                 {"code": "S01E03", "premiere_date": None}],
    )
    # 真实站内通知通道（不配 PushPlus，避免外部网络）；写入独立测试 DB。
    monkeypatch.setattr(
        scan_mod, "notifier",
        NotifierChain([InAppNotifier(session_factory=db)]),
    )
    mid = run(_seed_media(db, title="凡人修仙传"))

    async def _flow_errors():
        async with db() as s:
            return (
                await s.execute(
                    select(Notification).where(Notification.event_type == "flow_error")
                )
            ).scalars().all()

    async def _scenario():
        counts = []
        for _ in range(4):
            await scan_mod.scan_media(mid)  # 真实链路（外部服务已 mock）
            counts.append(len(await _flow_errors()))

        # 运行日志：穿过 routers/logs.py 的公开查询路径读回
        async with db() as s:
            page = await list_logs(
                admin=types.SimpleNamespace(id=1),
                session=s,
                task_type="scan_media",
                status=None,
                media_id=mid,
                tmdb_id=None,
                title=None,
                limit=10,
                offset=0,
            )
        return counts, page

    counts, page = run(_scenario())

    # 1) 本轮归因：skip_reason=no_candidates 且 missing_aired_total>0
    assert page["total"] == 4
    latest = page["items"][0]
    assert latest["scan_detail"]["skip_reason"] == "no_candidates"
    assert latest["scan_detail"]["missing_aired_total"] == 3

    # 2) 连续 3 轮告警一次；第 4 轮不重复（通知表 flow_error 计数）
    assert counts == [0, 0, 1, 1]

    # 3) 运行日志可读：中文结论 + scan_detail 可解析 + 5 阶段结构完整
    assert latest["media_title"] == "凡人修仙传"
    assert "存在已播缺失" in latest["message"]
    assert "本轮未获取到任何资源" in latest["message"]
    assert isinstance(latest["scan_detail"], dict)
    assert isinstance(latest["phases"], dict)
    assert set(latest["phases"]) == set(scan_mod._PHASE_KEYS)
    assert all(p["status"] == "done" for p in latest["phases"].values())

    alerts = run(_flow_errors())
    assert len(alerts) == 1
    assert alerts[0].event_type == "flow_error"
    assert "凡人修仙传" in alerts[0].body and "3 集" in alerts[0].body
