"""scan 静默机制移除后单测（queue-flow-rework Task 3）。

背景：unmatched 静默机制已移除——缺失集搜索失败后**不再**写 `status='unmatched'`
+ `silent_until`，本轮跳过、下一轮全局巡检自然重试；`TaskQueue.status` 状态集
收敛为 pending/ready/error/done。本文件验证移除后的行为（对齐 n8n 语义：搜不到
下轮再来，不做 2 天休眠）。

覆盖（mock 外部依赖 emby/cloudsaver/tmdb，风格对齐 test_scan_run_phases.py）：
- task_queue 预置遗留 status='unmatched' + silent_until 未来行 → 静默预过滤**不再
  生效**：无论 manual True/False 都照常搜索（_search_and_rank 被调用、
  phases.search=done、unmatched_silent_skipped=0、message 不含「静默期」）
- 搜索确认无资源（候选 share 验证过但无匹配文件）→ task_queue **不写** unmatched
  行、不写 silent_until；scan_detail.unmatched_marked=0
- 缺失集可在下一轮巡检重新入队：本轮搜不到不写任何静默状态 → 下轮资源出现时
  正常入队（无需等 silent_until 到期）
- scan_detail 结构稳定：unmatched_silent_skipped / unmatched_marked 恒 0 保留键
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  注册全部 ORM 模型
from app.database import Base
from app.models import Media, TaskQueue, TaskRun


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


async def _seed_silenced_tq(db, mid, episodes, silent_until):
    """预置历史遗留 status='unmatched'（带 silent_until 未来）的行——Task 3 后
    静默预过滤已移除，这些行不应再导致搜索被跳过。"""
    now = _now()
    async with db() as s:
        for ep in episodes:
            s.add(TaskQueue(
                media_id=mid,
                episode=ep,
                status="unmatched",
                probe_attempt=1,
                silent_until=silent_until,
                error="搜索确认无网盘资源，静默 N 天后自动重试",
                created_at=now,
                updated_at=now,
            ))
        await s.commit()


def _file(name: str, size: int = 1024) -> dict:
    return {"fileName": name, "fileId": f"f_{name}", "fileIdToken": "ft",
            "isFolder": False, "size": size}


def _cs_search_item(title="测试剧 S01") -> list:
    """cloudsaver.search 返回：一个可验证的分享候选（share_code=abc123）。"""
    return [{
        "title": title,
        "cloud_links": [
            {"cloud_type": "quark", "link": "https://pan.quark.cn/s/abc123"},
        ],
    }]


def _patch_scan_env(monkeypatch, db, *, missing_codes=None, search=None, share_list=None):
    """scan 外部依赖全 mock（emby 基线/cloudsaver），风格对齐 test_scan_run_phases.py。

    - search: cloudsaver.search 返回值（候选分享）；None 时返回 []（无候选）。
    - share_list: cloudsaver.share_list 返回值（分享内文件）；None 时返回空 list。
    """
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    emby_mock = AsyncMock()
    if missing_codes is not None:
        emby_mock.return_value = missing_codes
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", emby_mock)
    monkeypatch.setattr(
        scan_mod.cloudsaver, "search",
        AsyncMock(return_value=search if search is not None else []),
    )
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


async def _read_runs(db, mid):
    async with db() as s:
        rows = (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.asc())
            )
        ).scalars().all()
        return rows


async def _read_tq(db, mid):
    async with db() as s:
        rows = (
            await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
                .order_by(TaskQueue.episode.asc())
            )
        ).scalars().all()
        return rows


# ---------------------------------------------------------------------------
# 1) 预置静默行（silent_until 未来）→ 静默预过滤不再生效，照常搜索
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("manual", [False, True])
def test_scan_one_searches_despite_silenced_rows(db, monkeypatch, manual):
    """task_queue 预置 status='unmatched' + silent_until 未来的历史遗留行 → Task 3
    后静默预过滤移除：无论 manual True/False 都照常搜索（不进入「均在静默期」跳过），
    _search_and_rank 被调用、phases.search=done、unmatched_silent_skipped=0。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    run(_seed_silenced_tq(db, mid, ["S01E01", "S01E02"], _now() + timedelta(days=2)))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01", "S01E02"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid, manual=manual))
    runs = run(_read_runs(db, mid))
    assert len(runs) == 1 and runs[0].id == rid
    tr = runs[0]

    # 搜索阶段真实执行（静默预过滤不再短路跳过）
    search_mock.assert_awaited_once()

    import json
    phases = json.loads(tr.phases)
    assert phases["check"]["status"] == "done"
    assert phases["search"]["status"] == "done"
    # 不再静默短路 → match/enqueue/finish 照常全流程走完
    for key in ("match", "enqueue", "finish"):
        assert phases[key]["status"] == "done", key

    detail = json.loads(tr.scan_detail)
    assert detail["failed_phase"] is None
    assert detail["search_status"] == "no_candidates"  # 搜索执行了但无候选
    assert detail["unmatched_silent_skipped"] == 0      # 结构稳定键，恒 0
    assert detail["unmatched_marked"] == 0              # 不再标记静默
    assert "静默期" not in tr.message
    assert "未找到缺失集" in tr.message


# ---------------------------------------------------------------------------
# 2) 无静默行 → 正常搜索
# ---------------------------------------------------------------------------

def test_scan_one_searches_when_no_tq_rows(db, monkeypatch):
    """task_queue 无任何行 → 正常搜索（_search_and_rank 被调用、phases.search=done），
    行为与改造前一致。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))

    scan_mod = _patch_scan_env(monkeypatch, db, missing_codes=["S01E01"])
    search_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(scan_mod, "_search_and_rank", search_mock)

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"

    search_mock.assert_awaited_once()

    import json
    phases = json.loads(tr.phases)
    assert phases["search"]["status"] == "done"


# ---------------------------------------------------------------------------
# 3) 搜索确认无资源 → 不写 unmatched / silent_until
# ---------------------------------------------------------------------------

def test_scan_one_search_failure_writes_no_unmatched(db, monkeypatch):
    """候选 share 验证成功但无匹配文件（确实搜过、确实无资源）→ Task 3 后不再写
    status='unmatched' 行、不写 silent_until：task_queue 该 media 无任何新增行，
    scan_detail.unmatched_marked=0。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))

    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing_codes=["S01E01"],
        search=_cs_search_item(),   # 1 个可验证候选，但 share_list 无匹配文件
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"

    # 搜索确实执行（候选 share-info 验证成功」），但无匹配 → 不产生任何 task_queue 行
    rows = run(_read_tq(db, mid))
    assert rows == [], f"搜索失败不应写 unmatched/silent_until，实际: {rows}"

    import json
    detail = json.loads(tr.scan_detail)
    assert detail["unmatched_marked"] == 0
    assert detail["unmatched_silent_skipped"] == 0


# ---------------------------------------------------------------------------
# 4) 缺失集可在下一轮巡检自然重试入队
# ---------------------------------------------------------------------------

def test_scan_one_missing_retried_next_round(db, monkeypatch):
    """本轮搜索确认无资源（share_list 空）→ 不写任何静默状态；下一轮巡检资源出现
    → 该缺失集正常重新入队（status='ready'），无需等待 silent_until 解锁。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))

    # 第一轮：无资源（候选验证成功但分享内没有匹配文件）
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing_codes=["S01E01"],
        search=_cs_search_item(),
        share_list={"list": []},
    )
    run(scan_mod._scan_one(mid))
    assert run(_read_tq(db, mid)) == []  # 未写静默/未入队

    # 第二轮：资源出现 → 照常入队（无静默期拦截）
    scan_mod = _patch_scan_env(
        monkeypatch, db,
        missing_codes=["S01E01"],
        search=_cs_search_item(),
        share_list={"list": [_file("S01E01.mkv")]},
    )
    rid = run(scan_mod._scan_one(mid))
    trs = run(_read_runs(db, mid))
    assert len(trs) == 2
    assert trs[-1].id == rid and trs[-1].status == "success"

    rows = run(_read_tq(db, mid))
    assert [(r.episode, r.status) for r in rows] == [("S01E01", "ready")]
    assert all(r.silent_until is None for r in rows)  # 全程未写 silent_until