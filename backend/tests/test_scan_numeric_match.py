"""S01E190 可发现性三项修复单测（match_missing 纯数字兜底 / share-info 感知上报 /
_expand_share_codes 去重）。

覆盖：
- match_missing 纯数字兜底：`190.mkv` 命中 `{"S01E190"}`；多季缺失歧义保护
  （{"S01E01","S02E05"} 时 `05.mkv` 不匹配）；数字超范围不匹配；
  既有 SxxExx / 第N集 规则不受影响（含三位集数 S01E100）。
- _expand_share_codes：同一分享码在多个条目/链接重复出现 → 去重唯一。
- _scan_one share-info 感知：
  · 全部候选 share-info 失败（candidates>0、ok=0）→ message「候选分享，验证均失败
    （分享可能已失效/过期）」、status=skipped、scan_detail.share_info_ok=0/share_info_fail=N
  · 部分成功（1 成功 19 失败）→ 仍走缺集兜底文案「未找到缺失集 … 的资源」。
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import Media, TaskQueue, TaskRun


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
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


async def _seed_media(db, *, title="凡人修仙传", status="tracking"):
    async with db() as s:
        media = Media(title=title, media_type="tv", tmdb_id=106449, status=status)
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_base(monkeypatch, db, *, missing_codes):
    """基础 mock：emby 基线返回 missing_codes、read_size_limits/transfer 空转。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    emby_mock = AsyncMock(return_value=missing_codes)
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", emby_mock)
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


async def _read_runs(db, mid):
    async with db() as s:
        rows = (
            await s.execute(select(TaskRun).where(TaskRun.media_id == mid)
                            .order_by(TaskRun.id.asc()))
        ).scalars().all()
        return rows


# ---------------------------------------------------------------------------
# 1) match_missing 纯数字兜底（规则边界）
# ---------------------------------------------------------------------------

def test_match_missing_pure_numeric_hits_single_season():
    """纯数字命名 `190.mkv` 且缺失集同属一季 → 命中 S01E190。"""
    from app.tasks.scan import match_missing
    assert match_missing("190.mkv", {"S01E190"}) == "S01E190"


def test_match_missing_pure_numeric_tag_prefix_and_dotted():
    """前置 [xxx] 标签忽略；多段文件名首数字块（190.2020.2160p）可取首个数字。"""
    from app.tasks.scan import match_missing
    assert match_missing("[高清]190.mkv", {"S01E190"}) == "S01E190"
    assert match_missing("190.2020.2160p.mkv", {"S01E190"}) == "S01E190"


def test_match_missing_pure_numeric_out_of_range():
    """数字超集号范围（missing 只有到 190，文件 999）→ 不命中其余规则影响为空。"""
    from app.tasks.scan import match_missing
    assert match_missing("999.mkv", {"S01E190"}) is None
    assert match_missing("191.mkv", {"S01E190"}) is None


def test_match_missing_pure_numeric_multi_season_guard():
    """多季缺失（S01/S02 并存）→ 纯数字不匹配（无法判定归属季，防歧义）。"""
    from app.tasks.scan import match_missing
    assert match_missing("05.mkv", {"S01E01", "S02E05"}) is None
    # 但季号明确时仍按既有规则精确匹配
    assert match_missing("S02E05.mkv", {"S01E01", "S02E05"}) == "S02E05"


def test_match_missing_existing_rules_unchanged():
    """既有 SxxExx（含三位集数）/ 第N集 规则不受纯数字兜底影响。"""
    from app.tasks.scan import match_missing
    assert match_missing("S01E190.mkv", {"S01E190"}) == "S01E190"
    assert match_missing("第190集.mkv", {"S01E190"}) == "S01E190"
    assert match_missing("S01E100.mkv", {"S01E100"}) == "S01E100"  # E100 三位
    # 中文数字不落入纯数字兜底
    assert match_missing("第一九零集.mkv", {"S01E190"}) is None


# ---------------------------------------------------------------------------
# 2) _expand_share_codes 去重
# ---------------------------------------------------------------------------

def test_expand_share_codes_dedup():
    """同一分享码多个条目/链接重复 → 结果唯一（保留首次 title）。"""
    from app.tasks.scan import _expand_share_codes
    results = [
        {"title": "a", "cloud_links": [
            {"cloud_type": "quark", "link": "https://pan.quark.cn/s/83025fed147e"},
        ]},
        {"title": "b", "cloud_links": [
            {"cloud_type": "quark", "link": "https://pan.quark.cn/s/83025fed147e"},
            {"cloud_type": "quark", "link": "https://pan.quark.cn/s/9b9b55d858bc"},
        ]},
    ]
    out = _expand_share_codes(results)
    assert len(out) == 2
    assert {c["share_code"] for c in out} == {"83025fed147e", "9b9b55d858bc"}
    # 83… 保留首次 title="a"
    assert next(c for c in out if c["share_code"] == "83025fed147e")["title"] == "a"


def test_expand_share_codes_filters_non_quark():
    """非 quark cloud_type 不提取。"""
    from app.tasks.scan import _expand_share_codes
    out = _expand_share_codes([
        {"title": "x", "cloud_links": [
            {"cloud_type": "baidu", "link": "https://pan.quark.cn/s/aaa"}],
         }])
    assert out == []


# ---------------------------------------------------------------------------
# 3) _scan_one share-info 验证感知
# ---------------------------------------------------------------------------

def test_scan_one_share_info_all_failed_reports_invalid_candidates(db, monkeypatch):
    """全部候选 share-info 失败（candidates>0, ok=0）→ message「候选分享，验证均失败
    （分享可能已失效/过期）」、skipped、scan_detail 计数。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=["S01E190"])
    candidates = [{"title": "凡人修仙传 (2020)", "share_code": c} for c in
                  ("83025fed147e", "9b9b55d858bc")]
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=candidates))
    # 全部 share-info 抛异常（复用 _scan_one 实际调用的封装函数）
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(side_effect=RuntimeError("cloudSaver 500")),
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "搜索到 2 个候选分享，验证均失败（分享可能已失效/过期），未能获取缺失集资源" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["share_info_ok"] == 0
    assert detail["share_info_fail"] == 2
    assert detail["missing_total"] == 1


def test_scan_one_share_info_partial_success_falls_back_to_missing(db, monkeypatch):
    """部分成功（1 ok / 19 fail）且无匹配 → 走缺集兜底文案「未找到缺失集 … 的资源」。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=["S01E190"])
    candidates = [{"title": "凡人修仙传 (2020)", "share_code": f"code{n:012x}"}
                  for n in range(20)]
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=candidates))
    calls = {"n": 0}

    async def fake_share_info(code: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"pwd_id": "pd", "stoken": "st", "receive_code": "", "fileSize": 1000}
        raise RuntimeError("cloudSaver 500")

    monkeypatch.setattr(scan_mod, "_cloudsaver_share_info", fake_share_info)
    # 成功分享 share-list 返回空（无文件）→ 无匹配计数 → 缺集兜底
    monkeypatch.setattr(scan_mod.cloudsaver, "share_list",
                        AsyncMock(return_value={"list": []}))

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert "未找到缺失集 S01E190 的资源" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["share_info_ok"] == 1
    assert detail["share_info_fail"] == 19
    assert detail["failed_phase"] is None


def test_scan_one_share_info_all_failed_with_numeric_share_ok(db, monkeypatch):
    """共享全失败 message 不干扰真命中：即使候选仅 1 个、分享验证成功且文件 190.mkv
    存在 → 纯数字兜底命并入队 success。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=["S01E190"])
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": "凡人修仙传 (2020)", "share_code": "2c16748e7818"}]))
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                                "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [
            {"fileName": "190.mkv", "fileId": "f1", "fileIdToken": "ft",
             "isFolder": False, "size": 1024 * 1024 * 1024},
        ]}),
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "success"
    assert "已入队 1 个资源" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 1
    assert detail["share_info_ok"] == 1
    assert detail["share_info_fail"] == 0


def test_scan_one_skips_dead_candidates_reaches_valid_share(db, monkeypatch):
    """前 20 候选 share-info 全部失效、后续候选有效 → 越过错码、walk 有效分享
    （含 190.mkv）→ 纯数字兜底命并入队 success。

    线上案例复现：candidates 排序后前 20（乃至前 60）全为失效分享码，真实有效
    分享 2c16748e7818 排名靠后——验证前截断前 20 会永远轮不到有效分享。
    """
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=["S01E190"])
    # 25 个候选：前 20 个失效分享码（share-info 抛错）+ 第 21 个起有效
    dead = [{"title": "失效分享", "share_code": f"dead{i:012x}"} for i in range(20)]
    valid = [{"title": "凡人修仙传 (2020)", "share_code": f"ok{i:012x}"} for i in range(5)]
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(return_value=dead + valid))

    ok_codes = {c["share_code"] for c in valid}

    async def fake_share_info(code: str) -> dict:
        if code in ok_codes:
            return {"pwd_id": "pd", "stoken": "st", "receive_code": "", "fileSize": 9999}
        raise RuntimeError("cloudSaver 500")

    monkeypatch.setattr(scan_mod, "_cloudsaver_share_info", fake_share_info)
    # 有效分享 walk 返回 190.mkv（纯数字命名）→ _walk_share 根目录一层即命中
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [
            {"fileName": "190.mkv", "fileId": "f1", "fileIdToken": "ft",
             "isFolder": False, "size": 1024 * 1024 * 1024},
        ]}),
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "success"
    assert "已入队 1 个资源" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 1
    assert detail["share_info_ok"] == 5     # 越过 20 失效码，5 个有效全验证
    assert detail["share_info_fail"] == 20
    assert detail["search_status"] == "ok"
    # 入队落库（Task 2：扫描只写 task_queue，status='ready'，待下载队列取件）
    async def _read_tq():
        from sqlalchemy import select
        async with db() as s:
            return (await s.execute(
                select(TaskQueue).where(TaskQueue.media_id == mid)
            )).scalars().all()
    tq = run(_read_tq())
    assert len(tq) == 1 and tq[0].episode == "S01E190"
    assert tq[0].file_name == "190.mkv"  # 凭据快照携带分享原始文件名
    assert tq[0].status == "ready"  # 入队即就绪（探测已完成、凭据已收集）


def test_scan_one_share_try_limit_stops_when_all_dead(db, monkeypatch):
    """尝试上限 _MAX_SHARE_TRY 生效：候选 > 上限且全部失效 → 只验证上限个即停；
    message「验证均失败」N 用实际尝试数（=share_info_fail=上限）。"""
    from app.tasks import scan as scan_mod

    mid = run(_seed_media(db))
    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=["S01E190"])
    limit = scan_mod._MAX_SHARE_TRY
    candidates = [{"title": "失效分享", "share_code": f"dead{i:012x}"}
                  for i in range(limit + 15)]  # 超过上限
    monkeypatch.setattr(scan_mod, "_search_and_rank",
                        AsyncMock(return_value=candidates))
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(side_effect=RuntimeError("cloudSaver 500")),
    )

    rid = run(scan_mod._scan_one(mid))
    tr = run(_read_runs(db, mid))[0]
    assert tr.id == rid and tr.status == "skipped"
    assert f"搜索到 {limit} 个候选分享，验证均失败" in tr.message  # N = 实际尝试数
    detail = json.loads(tr.scan_detail)
    assert detail["share_info_ok"] == 0
    assert detail["share_info_fail"] == limit   # 未验证剩余候选（超上限）