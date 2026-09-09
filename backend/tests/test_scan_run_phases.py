"""巡检可见性改造单测（task_run running 中间态 + 5 阶段进度 + 结果摘要 + 人话 message）。

覆盖（mock 外部依赖 emby/cloudsaver/tmdb，风格对齐 test_scan_baseline.py）：
- _scan_one 两段式：触发先建 running 记录、结束后原地 UPDATE 同一条为终态
  （success/skipped/error），无重复记录
- phases：正常全流程 5 阶段键齐备全部 done；中途失败阶段定位 error +
  scan_detail.failed_phase（Emby 基线失败→check；cloudSaver 搜索异常→search）；
  正常短路径（跳过）阶段标记 skipped、终态不残留 wait
- scan_detail：missing_items 初始 not_found → 入队成功改 enqueued；计数准确
- 搜索故障 vs 无候选（message 语义修复）：全关键词失败 → _search_and_rank 抛
  ScanSearchUnavailable → status=error + search 阶段 error + failed_phase="search" +
  message「搜索服务异常…」；搜索成功但无候选且缺集 >0 → message
  「未找到缺失集 S01E0X 的资源（搜索无匹配候选…）」；单词失败降级语义保持
- 人话 message：enqueued>0「已入队 N 个资源…」；enqueued=0&unmatched>0
  「未找到缺失集资源…」；skipped/error 沿用既有中文文案
- GET /api/logs/{id}（含 phases/scan_detail/media_title）与 GET /api/queue
  父级 scan_tasks 返回结构（批量取最近 1 条，孤儿父级空数组）
"""
import asyncio
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import DownloadQueue, Media, TaskQueue, TaskRun


def run(coro):
    return asyncio.run(coro)


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


async def _seed_media(db, *, title="测试剧", media_type="tv", status="tracking"):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=42, status=status)
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_env(monkeypatch, db, *, missing_codes=None, search=None, share_list=None,
                    candidates=None):
    """scan 外部依赖全 mock：emby 基线返回 missing_codes、cloudSaver 返回固定结果。

    - missing_codes: AsyncMock 的 return_value（list）或 side_effect（抛异常）
    - candidates: 直接替换 _search_and_rank 的返回候选（share_code 去重可控）。
      为 None 时真实跑 _search_and_rank（tv 会按季词+纯标题多次搜索 → 候选可能重复
      → share 被遍历多遍），测试计数易失真，故固定候选路径优先。
    """
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    emby_mock = AsyncMock()
    if missing_codes is not None:
        emby_mock.return_value = missing_codes
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", emby_mock)
    if candidates is None:
        monkeypatch.setattr(
            scan_mod.cloudsaver, "search",
            AsyncMock(return_value=search if search is not None else []),
        )
    else:
        monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                               "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value=share_list if share_list is not None else {"list": []}),
    )
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


def _cs_item(title="测试剧 S01") -> dict:
    return {
        "title": title,
        "cloud_links": [{"cloud_type": "quark", "link": "https://pan.quark.cn/s/abc123"}],
    }


def _file(name: str, size: int = 1024) -> dict:
    return {"fileName": name, "fileId": f"f_{name}", "fileIdToken": "ft",
            "isFolder": False, "size": size}


async def _read_runs(db, mid):
    async with db() as s:
        rows = (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.asc())
            )
        ).scalars().all()
        return rows


# ---------------------------------------------------------------------------
# 1) 两段式：先 running 中间态 → 结束原地 UPDATE 同一条为终态
# ---------------------------------------------------------------------------

def test_scan_one_starts_running_and_finishes_same_row(db, monkeypatch):
    """触发即建 running 记录；基线阶段阻塞期间可见 running；结束后同一条变终态，
    不新增第二行。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_missing(media):
        entered.set()
        await release.wait()
        return ["S01E01"]

    monkeypatch.setattr(scan_mod, "async_session", db)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", fake_missing)
    monkeypatch.setattr(scan_mod.cloudsaver, "search",
                        AsyncMock(return_value=[_cs_item()]))
    monkeypatch.setattr(scan_mod.cloudsaver, "share_info",
                        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st",
                                               "receive_code": "", "fileSize": 9999}))
    monkeypatch.setattr(scan_mod.cloudsaver, "share_list",
                        AsyncMock(return_value={"list": [_file("S01E01.mkv")]}))
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))

    async def _case():
        task = asyncio.create_task(scan_mod._scan_one(mid))
        await entered.wait()
        # 基线（网络 IO）阻塞中：running 中间态应已可见
        async with db() as s:
            runs = (
                await s.execute(select(TaskRun).where(TaskRun.media_id == mid))
            ).scalars().all()
        assert len(runs) == 1
        assert runs[0].status == "running"
        assert runs[0].message == "正在巡检"
        release.set()
        rid = await task
        # 结束后：同一条记录变终态，无新增行
        async with db() as s:
            rows = (
                await s.execute(select(TaskRun).where(TaskRun.media_id == mid))
            ).scalars().all()
        assert len(rows) == 1 and rows[0].id == rid
        assert rows[0].status == "success"
        assert rows[0].finished_at is not None
        assert rows[0].duration_seconds is not None

    run(_case())


def test_scan_one_skipped_paused_updates_same_row(db, monkeypatch):
    """paused 跳过：先建 running 再原地更新为 skipped（单行终态，沿用中文文案）。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db, status="paused"))
    scan_mod = _patch_scan_env(monkeypatch, db)  # 基线不会被执行（状态预检在前）

    rid = run(scan_mod._scan_one(mid))

    runs = run(_read_runs(db, mid))
    assert len(runs) == 1 and runs[0].id == rid
    assert runs[0].status == "skipped"
    assert "media.status=paused，跳过巡检" in runs[0].message
    assert runs[0].phases is not None
    assert runs[0].scan_detail is None  # 短路分支无结果摘要


# ---------------------------------------------------------------------------
# 2) phases：5 阶段键 + 全流程 done / 失败阶段 error + failed_phase
# ---------------------------------------------------------------------------

def test_scan_one_success_phases_all_done_and_scan_detail(db, monkeypatch):
    """tv 缺失 2 集 + 1 个无关文件：全部 5 阶段 done；missing_items 入队标记 enqueued、
    未匹配文件计 unmatched；人话 message。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    share_list = {"list": [
        _file("S01E01.mkv"), _file("S01E02.mkv"), _file("S03E99.mkv"),
    ]}
    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01", "S01E02"],
                               candidates=[{"title": "测试剧 S01", "share_code": "abc123"}],
                               share_list=share_list)

    rid = run(scan_mod._scan_one(mid))
    runs = run(_read_runs(db, mid))
    tr = runs[0]
    assert tr.id == rid and tr.status == "success"

    import json
    phases = json.loads(tr.phases)
    assert list(phases.keys()) == ["check", "search", "match", "enqueue", "finish"]
    for key, ph in phases.items():
        assert ph["status"] == "done", key
        assert ph["started_at"] and ph["finished_at"]  # 打点时间均落库

    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] is None
    assert detail["missing_total"] == 2
    assert detail["enqueued"] == 2
    assert detail["unmatched"] == 1
    assert detail["size_filtered"] == 0 and detail["non_video"] == 0
    by_ep = {item["episode"]: item["result"] for item in detail["missing_items"]}
    assert by_ep == {"S01E01": "enqueued", "S01E02": "enqueued"}

    assert "已入队 2 个资源" in tr.message
    assert "1 个文件未匹配" in tr.message
    # §4.1：unmatched_marked 键存在（本轮未匹配集未被标静默，matched 全命中）
    assert "unmatched_marked" in detail

    # 入队只写 task_queue（queue-flow-rework Task 2：status='ready'，凭据收集完毕；
    # 同步 promote 双写 download_queue 已移除，下载队列后续从 task_queue 取件）
    async def _read_tables():
        async with db() as s:
            dq = (await s.execute(select(DownloadQueue).where(DownloadQueue.media_id == mid))).scalars().all()
            tq = (await s.execute(select(TaskQueue).where(TaskQueue.media_id == mid))).scalars().all()
            return dq, tq
    dq, tq = run(_read_tables())
    assert {t.episode for t in tq} == {"S01E01", "S01E02"}
    assert all(t.status == "ready" for t in tq)
    assert dq == []  # 不再同步写 download_queue


def test_scan_one_emby_failure_failed_phase_check(db, monkeypatch):
    """Emby 基线抛异常 → status=error、check 阶段 error、failed_phase='check'、
    文案沿用「Emby 故障，fail-safe…」。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    # 复用 _patch_scan_env 打底（含 async_session→db、其余外部 mock），再覆写基线为异常
    _patch_scan_env(monkeypatch, db, missing_codes=None)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes",
                        AsyncMock(side_effect=RuntimeError("emby down")))

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "error"
    assert "Emby 故障，fail-safe 暂停新缺集发现: emby down" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "error"
    # check 之后的阶段保持 wait（错误中止非主动跳过）→ 无残留 process
    for key in ("search", "match", "enqueue", "finish"):
        assert phases[key]["status"] == "wait", key
    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] == "check"


def test_scan_one_search_exception_failed_phase_search(db, monkeypatch):
    """cloudSaver 搜索阶段抛异常 → search 阶段 error、failed_phase='search'、
    message「搜索服务异常…」，status=error。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    # check done 后，搜索阶段整体异常（_search_and_rank 被替换为抛错）
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(side_effect=RuntimeError("search boom")))

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "error"
    assert "搜索服务异常（cloudSaver 不可达或超时），未能查找缺失集资源，请稍后重试" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "done"
    assert phases["search"]["status"] == "error"
    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] == "search"
    assert detail["search_status"] == "failed"


def test_scan_one_all_keywords_failed_search_error(db, monkeypatch):
    """全关键词失败（规格场景）：_build_keywords 产生 ≥1 词（tv 季词+纯标题），所有词
    cloudsaver.search 均抛 CloudSaverUnavailable → _search_and_rank 抛
    ScanSearchUnavailable → _scan_one：status=error、phases.search=error、
    scan_detail.failed_phase="search"、message 含「搜索服务异常」。"""
    from app.services.cloudsaver import CloudSaverUnavailable
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    # 走真实 _search_and_rank：所有关键词（「测试剧 S01」+「测试剧」≥1 个）均失败
    monkeypatch.setattr(
        scan_mod.cloudsaver, "search",
        AsyncMock(side_effect=CloudSaverUnavailable("cloudSaver 不可达")),
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "error"
    assert "搜索服务异常（cloudSaver 不可达或超时），未能查找缺失集资源，请稍后重试" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "done"
    assert phases["search"]["status"] == "error"
    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] == "search"
    assert detail["search_status"] == "failed"


def test_scan_one_single_keyword_failure_still_succeeds(db, monkeypatch):
    """部分关键词失败降级：一个词抛异常、一个词成功（返回无候选）→ 不抛、正常返回
    skipped + 缺集文案（保持 test_scan_search_keywords 的单词降级语义在 _scan_one
    端到端仍成立）。"""
    from app.services.cloudsaver import CloudSaverUnavailable
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])

    async def fake_search(kw: str):
        if "S01" in kw:
            raise CloudSaverUnavailable("cloudSaver 不可达")
        return []  # 纯标题词成功但无结果

    monkeypatch.setattr(scan_mod.cloudsaver, "search", fake_search)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"  # 非 error：存在成功关键词
    assert "未找到缺失集 S01E01 的资源" in tr.message
    import json
    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] is None
    assert detail["search_status"] == "no_candidates"


def test_scan_one_no_missing_skipped_phases_no_wait_residue(db, monkeypatch):
    """无遗漏跳过（短路径）：check done，后续阶段 skipped（不残留 wait）；scan_detail
    存在且 failed_phase=None；文案沿用「无遗漏集…」。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=[])

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "无遗漏集（Emby 基线已覆盖），跳过" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "done"
    for key in ("search", "match", "enqueue", "finish"):
        assert phases[key]["status"] == "skipped", key  # 终态无 wait 残留
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 0
    assert detail["failed_phase"] is None


# ---------------------------------------------------------------------------
# 3) 人话 message
# ---------------------------------------------------------------------------

def test_scan_one_human_message_unmatched_zero_enqueued(db, monkeypatch):
    """搜索到文件但全不匹配（enqueued=0, unmatched>0）→「未找到缺失集资源…」+ 计数；
    status=skipped。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    share_list = {"list": [
        _file("凡人修仙传 第500集.mkv"),   # 集号与缺失集 S01E01 不匹配 → unmatched
    ]}
    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"],
                               candidates=[{"title": "凡人修仙传 (2020)", "share_code": "abc123"}],
                               share_list=share_list)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "未找到缺失集资源（搜索到 1 个文件均不匹配" in tr.message
    assert "多为已收录旧集或其它版本，可能尚未更新" in tr.message


def test_scan_one_human_message_enqueued_with_filters(db, monkeypatch):
    """入队 + 大小过滤 + 非视频计数：人话 message 携带存在的分项。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    share_list = {"list": [
        _file("S01E01.mkv", 1024),      # 入队
        _file("S01E02.mkv", 1024 * 1024 * 1024 * 500),  # 超大小上限(100G) → size_filtered
        _file("Cover.jpg", 2048),       # 非视频
    ]}
    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01", "S01E02"],
                               candidates=[{"title": "测试剧 S01", "share_code": "abc123"}],
                               share_list=share_list)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "success"
    assert "已入队 1 个资源" in tr.message
    assert "1 个超大小限制跳过" in tr.message
    assert "1 个非视频" in tr.message

    import json
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 1
    assert detail["size_filtered"] == 1
    assert detail["non_video"] == 1
    assert detail["unmatched"] == 0


def test_scan_one_human_message_no_candidates_with_missing(db, monkeypatch):
    """搜索成功但无候选且存在缺失集 S01E01（线上案例：candidates 空、parts 空）→
    message 明示「未找到缺失集 S01E01 的资源…」（不再误导性「无候选命中」）；
    phases 5 阶段全部 done、scan_detail.missing_total=1、status=skipped。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    # 搜索成功（≥1 关键词 ok）但云分享无候选 → _search_and_rank 返回 []（不抛）
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[]))

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "未找到缺失集 S01E01 的资源" in tr.message
    assert "搜索无匹配候选，资源可能尚未更新" in tr.message

    import json
    phases = json.loads(tr.phases)
    assert list(phases.keys()) == ["check", "search", "match", "enqueue", "finish"]
    assert all(phases[k]["status"] == "done" for k in phases)
    detail = json.loads(tr.scan_detail)
    assert detail["missing_total"] == 1
    assert detail["failed_phase"] is None
    assert detail["search_status"] == "no_candidates"
    assert detail["missing_items"] == [{"episode": "S01E01", "result": "not_found"}]


# ---------------------------------------------------------------------------
# 4) API：/api/logs/{id} 与 /api/queue scan_tasks
# ---------------------------------------------------------------------------

@pytest.fixture()
def _router_db():
    """独立 in-memory SQLite（create_all 最新结构）→ sessionmaker。"""
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


def test_logs_detail_returns_full_record_with_phases(_router_db):
    """GET /api/logs/{id}：单条完整记录含 phases/scan_detail（JSON dict）与 media_title。"""
    from app.routers.logs import get_log, list_logs

    async def _seed():
        async with _router_db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            run_row = TaskRun(
                task_type="scan_media", media_id=media.id, status="success",
                message="已入队 1 个资源。", started_at=_now(), finished_at=_now(),
                duration_seconds=1.2,
                phases='{"check": {"status": "done", "started_at": "2026-09-07T00:00:00", "finished_at": null}}',
                scan_detail='{"missing_total": 1, "enqueued": 1, "failed_phase": null, '
                            '"missing_items": [{"episode": "S01E01", "result": "enqueued"}]}',
            )
            s.add(run_row)
            await s.commit()
            return run_row.id, media.id

    run_id, media_id = run(_seed())

    async def _case():
        async with _router_db() as s:
            detail = await get_log(run_id, admin=MagicMock(), session=s)
            assert detail["id"] == run_id
            assert detail["media_title"] == "测试剧"
            assert detail["phases"]["check"]["status"] == "done"
            assert detail["scan_detail"]["missing_total"] == 1
            assert detail["scan_detail"]["missing_items"][0]["result"] == "enqueued"
            # 404：不存在
            from fastapi import HTTPException
            try:
                await get_log(999999, admin=MagicMock(), session=s)
                raise AssertionError("应 404")
            except HTTPException as exc:
                assert exc.status_code == 404
            # list_logs 亦附 phases/scan_detail
            rows = await list_logs(
                admin=MagicMock(), session=s,
                task_type=None, status=None, media_id=media_id, tmdb_id=None, title=None,
                limit=50, offset=0,
            )
            assert len(rows) == 1
            assert rows[0]["phases"]["check"]["status"] == "done"
            assert rows[0]["scan_detail"]["enqueued"] == 1

    run(_case())


def test_logs_detail_media_deleted_title_none(_router_db):
    """media 记录已删（孤儿 task_run）→ 单条详情 media_title=None，phases=None。"""
    from app.routers.logs import get_log

    async def _seed():
        async with _router_db() as s:
            run_row = TaskRun(
                task_type="scan_media", media_id=999999, status="success",
                message="x", started_at=_now(),
                duration_seconds=0.1, phases=None, scan_detail=None,
            )
            s.add(run_row)
            await s.commit()
            return run_row.id

    run_id = run(_seed())

    async def _case():
        async with _router_db() as s:
            detail = await get_log(run_id, admin=MagicMock(), session=s)
            assert detail["media_id"] == 999999
            assert detail["media_title"] is None
            assert detail["phases"] is None and detail["scan_detail"] is None

    run(_case())


def test_queue_scan_tasks_latest_only_and_orphan_empty(_router_db):
    """GET /api/queue：扁平任务列表无 scan_tasks/children 等树字段；
    孤儿 media_id 直接以扁平原样返回。"""
    from app.routers.queue import list_queue

    now = _now()

    async def _seed():
        async with _router_db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=42, status="tracking")
            s.add(media)
            await s.flush()
            mid = media.id
            # 活跃 download_queue 行直接出现在扁平列表中
            s.add(DownloadQueue(media_id=mid, episode="S01E01", status="pending",
                                file_name="S01E01.mkv", file_size=1024,
                                share_code="sc", stoken="s", receive_code="r",
                                fids="[]", fid_tokens="[]", folder_id="f",
                                updated_at=now))
            # 巡检记录不影响队列列表展示
            s.add(TaskRun(task_type="scan_media", media_id=mid, status="success",
                          message="旧巡检", started_at=now - timedelta(days=1),
                          duration_seconds=3.0))
            s.add(TaskRun(task_type="scan_media", media_id=mid, status="running",
                          message="正在巡检", started_at=now,
                          duration_seconds=None))
            # 孤儿 download_queue（media 不存在）：FK 在 SQLite 无 enforcement，可插入
            s.add(DownloadQueue(media_id=999999, episode="S99E99", status="pending",
                                file_name="orphan.mkv", file_size=1,
                                share_code="sc", stoken="s", receive_code="r",
                                fids="[]", fid_tokens="[]", folder_id="f",
                                updated_at=now))
            await s.commit()
            return mid

    mid = run(_seed())

    async def _case():
        async with _router_db() as s:
            res = await list_queue(user=MagicMock(), session=s, limit=100, offset=0)
            rows = res["items"]
            assert res["total"] == 2  # 两条活跃 dq（真实 media + 孤儿），巡检 TaskRun 不参与
            by_id = {r["media_id"]: r for r in rows}
            # 真实 media 行以扁平原样返回，无树字段
            parent = by_id[mid]
            assert parent["title"] == "测试剧"
            assert parent["episode"] == "S01E01"
            assert "scan_tasks" not in parent
            assert "children" not in parent
            assert "aggregate_status" not in parent
            # 孤儿 media_id 直接以扁平原样返回
            orphan = next(r for r in rows if r["media_id"] == 999999)
            assert orphan["title"] is None
            assert orphan["episode"] == "S99E99"

    run(_case())
