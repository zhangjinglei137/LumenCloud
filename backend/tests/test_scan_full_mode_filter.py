"""全量模式文件级校验单测（少帅式搜索误匹配修复 A1/A2/A3/A4）。

背景（线上案例）：搜索「少帅」（TV 48 集）时一次入队 284 个错误资源（同名短剧
「少帅将我宠上天(99集)」、纯数字 9.mp4/116.mp4 等）。根因：Emby 未收录 →
scan_baseline_required=False 全量模式 → 每个搜索到的文件零校验直接入队；
_rank_candidates 只加分排序不过滤。修复：
- A1 _share_title_relevant：分享标题与剧名完全无关 → 排序前剔除；
- A2 _full_mode_accept：全量模式逐文件校验（强制可解析集号）；
- A3 _media_total_episodes：TMDB 总集数（超集号拦截）；
- A4 _FULL_MODE_ENQUEUE_LIMIT：全量单轮入队限批。

覆盖：
- _share_title_relevant：精确/前缀/子串 True；完全无关 False；空 title True
- _parse_episode_number：SxxExx/第N集/独立数字 token 解析；(99集) 等不解析
- _full_mode_accept（media=少帅,total=48）边界矩阵 + total=None 降级
- 集成：tv 全量模式 scan 只入队通过校验的文件、超集号被拒、限批生效
- 回归：真电影全量模式不受 A2/A3/A4 影响（保留原行为）
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.database import Base
from app.models import DownloadQueue, Media, TaskRun


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


async def _seed_media(db, *, title="少帅", media_type="tv", status="tracking"):
    async with db() as s:
        media = Media(title=title, media_type=media_type, tmdb_id=456, status=status)
        s.add(media)
        await s.commit()
        return media.id


def _patch_scan_base(monkeypatch, db, *, missing_codes=None):
    """基础 mock：emby 基线返回 missing_codes、read_size_limits/transfer 空转。"""
    from app.tasks import scan as scan_mod

    monkeypatch.setattr(scan_mod, "async_session", db)
    emby_mock = AsyncMock(return_value=missing_codes if missing_codes is not None else [None])
    monkeypatch.setattr(scan_mod, "_emby_missing_codes", emby_mock)
    monkeypatch.setattr(scan_mod, "_read_size_limits", AsyncMock(return_value=(100.0, 100.0)))
    monkeypatch.setattr(scan_mod, "_trigger_transfer", AsyncMock(return_value=None))
    return scan_mod


def _share_file(name: str) -> dict:
    """share-list 单文件条目（对齐 _walk_share 输入契约，isFolder=False）。"""
    return {
        "fileName": name, "fileId": f"f_{name}", "fileIdToken": f"ft_{name}",
        "isFolder": False, "size": 1024 * 1024 * 1024,
    }


def _patch_tv_full_mode_env(monkeypatch, db, *, files, total_episodes: int | None = 48,
                            title="少帅", limit=None):
    """tv 未收录全量（movie_missing）端到端 mock：单候选分享 + 固定文件列表。"""
    from app.tasks import scan as scan_mod

    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=[None])
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": f"{title} 全量", "share_code": "2c16748e7818"},
    ]))
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                                "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [_share_file(n) for n in files]}),
    )
    # A3：TMDB 总集数（mock 掉真实回源；None 模拟降级）
    monkeypatch.setattr(scan_mod, "_media_total_episodes",
                        AsyncMock(return_value=total_episodes))
    if limit is not None:
        monkeypatch.setattr(scan_mod, "_FULL_MODE_ENQUEUE_LIMIT", limit)
    return scan_mod


async def _read_downloads(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(DownloadQueue).where(DownloadQueue.media_id == mid)
                .order_by(DownloadQueue.episode.asc())
            )
        ).scalars().all()


async def _read_last_run(db, mid):
    async with db() as s:
        return (
            await s.execute(
                select(TaskRun).where(TaskRun.media_id == mid)
                .order_by(TaskRun.id.desc())
            )
        ).scalars().first()


# ---------------------------------------------------------------------------
# 1) A1 _share_title_relevant：分享标题相关性
# ---------------------------------------------------------------------------

def test_share_title_relevant_exact_prefix_substring():
    from app.tasks.scan import _share_title_relevant
    # 精确相等
    assert _share_title_relevant("少帅", "少帅") is True
    # 前缀（cand 以 title 开头）
    assert _share_title_relevant("少帅", "少帅全集") is True
    # 子串（title 是 cand 的子串）
    assert _share_title_relevant("少帅", "2024少帅蓝光 4K") is True
    # 空格/大小写归一化后仍命中
    assert _share_title_relevant("少 帅", "少帅2024") is True


def test_share_title_relevant_unrelated_false():
    from app.tasks.scan import _share_title_relevant
    # 完全无关
    assert _share_title_relevant("少帅", "凡人修仙传合集") is False
    assert _share_title_relevant("少帅", "与凤行 1080p") is False


def test_share_title_relevant_empty_title_tolerant():
    from app.tasks.scan import _share_title_relevant
    # 剧名为空 → 兜底不阻断
    assert _share_title_relevant("", "凡人修仙传合集") is True
    # 候选标题为空 → 兜底放行（避免误杀无标题候选）
    assert _share_title_relevant("少帅", "") is True


# ---------------------------------------------------------------------------
# 2) _parse_episode_number：文件名集号解析
# ---------------------------------------------------------------------------

def test_parse_episode_number_rules():
    from app.tasks.scan import _parse_episode_number
    # ① SxxExx
    assert _parse_episode_number("S01E30.mkv") == 30
    # ② 第N集
    assert _parse_episode_number("第30集.mkv") == 30
    # ③ 独立数字 token
    assert _parse_episode_number("30.mp4") == 30
    assert _parse_episode_number("[字幕组]116.mp4") == 116
    assert _parse_episode_number("少帅 30.mkv") == 30
    assert _parse_episode_number("少帅 9.mp4") == 9
    # 多段文件名取首个数字块（对齐 match_missing 纯数字兜底）
    assert _parse_episode_number("190.2020.2160p.mkv") == 190


def test_parse_episode_number_no_number():
    from app.tasks.scan import _parse_episode_number
    # 紧贴中文的集数标记（无「第」）不识别为集号
    assert _parse_episode_number("少帅将我宠上天(99集).mp4") is None
    assert _parse_episode_number("少帅将我宠上天99集.mp4") is None
    # 无集号文件名
    assert _parse_episode_number("少帅娇宠特工妻-合成版.mp4") is None
    assert _parse_episode_number("") is None


# ---------------------------------------------------------------------------
# 3) _full_mode_accept：A2/A3 核心判定（media=少帅, total=48）
# ---------------------------------------------------------------------------

def _media(title="少帅"):
    return SimpleNamespace(title=title, media_type="tv", tmdb_id=456, id=1)


def test_full_mode_accept_standard_naming_in_range():
    from app.tasks.scan import _full_mode_accept
    m = _media()
    assert _full_mode_accept(m, "少帅.S01E30.mkv", 48) is True
    assert _full_mode_accept(m, "少帅 第30集.mkv", 48) is True
    assert _full_mode_accept(m, "S01E30.mkv", 48) is True  # 标准命名无剧名亦可


def test_full_mode_accept_rejects_pure_numeric_without_title():
    from app.tasks.scan import _full_mode_accept
    m = _media()
    assert _full_mode_accept(m, "9.mp4", 48) is False   # 纯数字无剧名
    assert _full_mode_accept(m, "116.mp4", 48) is False


def test_full_mode_accept_rejects_out_of_range_and_no_ep():
    from app.tasks.scan import _full_mode_accept
    m = _media()
    # 超集号（同名短剧 99>48）
    assert _full_mode_accept(m, "少帅将我宠上天(99集).mp4", 48) is False
    # 标准命名但超集号
    assert _full_mode_accept(m, "少帅.S01E99.mkv", 48) is False
    # 含剧名但无集号
    assert _full_mode_accept(m, "少帅娇宠特工妻-合成版.mp4", 48) is False


def test_full_mode_accept_title_plus_number():
    from app.tasks.scan import _full_mode_accept
    m = _media()
    # 含剧名的独立数字（少帅 9.mp4）且集号在范围内 → 入队
    assert _full_mode_accept(m, "少帅 9.mp4", 48) is True
    # 含剧名但数字超集号 → 拒绝
    assert _full_mode_accept(m, "少帅 99.mp4", 48) is False


def test_full_mode_accept_total_none_degrade():
    from app.tasks.scan import _full_mode_accept
    m = _media()
    # TMDB 降级（total=None）：纯数字仍拒绝（无剧名无标准命名）
    assert _full_mode_accept(m, "9.mp4", None) is False
    # 标准命名放行（无集号上限）
    assert _full_mode_accept(m, "少帅.S01E30.mkv", None) is True


# ---------------------------------------------------------------------------
# 4) 集成：tv 全量模式 scan 文件级校验 + 超集号拦截
# ---------------------------------------------------------------------------

def test_scan_tv_full_mode_filters_unrelated_files(db, monkeypatch):
    """tv 未收录全量：只入队通过 _full_mode_accept 的文件；无关/超集号文件被拒并计数。"""
    files = [
        "少帅.S01E01.mkv",      # 标准命名 → 入队
        "少帅 第30集.mkv",       # 标准命名 → 入队
        "少帅将我宠上天(99集).mp4",  # 同名短剧（集号 99>48 / 无独立 token）→ 拒绝
        "9.mp4",                 # 纯数字无剧名 → 拒绝
        "少帅.S01E99.mkv",       # 标准命名但 99>48 → 拒绝
    ]
    scan_mod = _patch_tv_full_mode_env(monkeypatch, db, files=files, total_episodes=48)

    mid = run(_seed_media(db, title="少帅"))
    rid = run(scan_mod._scan_one(mid))

    tr = run(_read_last_run(db, mid))
    assert tr.id == rid and tr.status == "success"
    assert "已入队 2 个资源" in tr.message
    assert "3 个无关/超集号文件过滤" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 2
    assert detail["unrelated_filtered"] == 3
    # 落库的 download_queue 只有通过校验的 2 个文件
    dq = run(_read_downloads(db, mid))
    assert [row.episode for row in dq] == ["少帅 第30集.mkv", "少帅.S01E01.mkv"]
    assert all(row.status == "pending" for row in dq)


def test_scan_tv_full_mode_all_rejected_skipped(db, monkeypatch):
    """tv 未收录全量且全部文件被拒（无有效集号）→ 不入队，message 体现过滤计数。"""
    files = ["9.mp4", "116.mp4", "少帅娇宠特工妻-合成版.mp4"]
    scan_mod = _patch_tv_full_mode_env(monkeypatch, db, files=files, total_episodes=48)

    mid = run(_seed_media(db, title="少帅"))
    rid = run(scan_mod._scan_one(mid))

    tr = run(_read_last_run(db, mid))
    assert tr.id == rid and tr.status == "skipped"
    assert "3 个无关/超集号文件过滤" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 0
    assert detail["unrelated_filtered"] == 3
    assert run(_read_downloads(db, mid)) == []


def test_scan_tv_full_mode_limit_batch(db, monkeypatch):
    """A4 全量限批：mock _FULL_MODE_ENQUEUE_LIMIT=2，5 个有效文件只入队 2 个。"""
    files = [f"少帅.S01E0{i}.mkv" for i in range(1, 6)]  # 5 个全部通过校验
    scan_mod = _patch_tv_full_mode_env(monkeypatch, db, files=files,
                                       total_episodes=48, limit=2)

    mid = run(_seed_media(db, title="少帅"))
    rid = run(scan_mod._scan_one(mid))

    tr = run(_read_last_run(db, mid))
    assert tr.id == rid and tr.status == "success"
    assert "已入队 2 个资源" in tr.message
    assert "已达全量模式单轮入队上限 2" in tr.message
    detail = json.loads(tr.scan_detail)
    assert detail["enqueued"] == 2
    dq = run(_read_downloads(db, mid))
    assert [row.episode for row in dq] == ["少帅.S01E01.mkv", "少帅.S01E02.mkv"]


def test_scan_tv_full_mode_total_none_still_filters_pure_numeric(db, monkeypatch):
    """TMDB 降级（total=None）：纯数字仍拒绝；标准命名/含剧名+数字照常入队。"""
    files = ["9.mp4", "少帅.S01E01.mkv", "少帅 9.mp4"]
    scan_mod = _patch_tv_full_mode_env(monkeypatch, db, files=files, total_episodes=None)

    mid = run(_seed_media(db, title="少帅"))
    rid = run(scan_mod._scan_one(mid))

    tr = run(_read_last_run(db, mid))
    assert tr.id == rid and tr.status == "success"
    assert "已入队 2 个资源" in tr.message
    assert "1 个无关/超集号文件过滤" in tr.message
    dq = run(_read_downloads(db, mid))
    assert [row.episode for row in dq] == ["少帅 9.mp4", "少帅.S01E01.mkv"]


# ---------------------------------------------------------------------------
# 5) 回归：真电影全量模式不受 A2/A3/A4 影响（保留原行为）
# ---------------------------------------------------------------------------

def test_scan_movie_full_mode_unchanged(db, monkeypatch):
    """真电影（media_type=movie）全量模式：不套用 _full_mode_accept，文件照常入队。"""
    from app.tasks import scan as scan_mod

    scan_mod = _patch_scan_base(monkeypatch, db, missing_codes=[None])
    monkeypatch.setattr(scan_mod, "_search_and_rank", AsyncMock(return_value=[
        {"title": "大话西游", "share_code": "2c16748e7818"},
    ]))
    monkeypatch.setattr(
        scan_mod, "_cloudsaver_share_info",
        AsyncMock(return_value={"pwd_id": "pd", "stoken": "st", "receive_code": "",
                                "fileSize": 9999}),
    )
    monkeypatch.setattr(
        scan_mod.cloudsaver, "share_list",
        AsyncMock(return_value={"list": [_share_file("大话西游.2020.2160p.mkv"),
                                         _share_file("9.mp4")]}),
    )
    # movie 全量不调用 _media_total_episodes（保持原行为，无需 mock；防意外调用则报错）
    async def _explode(*args, **kwargs):
        raise AssertionError("movie 全量模式不应调用 _media_total_episodes")
    monkeypatch.setattr(scan_mod, "_media_total_episodes", _explode)

    mid = run(_seed_media(db, title="大话西游", media_type="movie"))
    rid = run(scan_mod._scan_one(mid))

    tr = run(_read_last_run(db, mid))
    assert tr.id == rid and tr.status == "success"
    # movie 全量：episode=文件名、matched_key=movie:<title> 归一化键（§7 P1）——两文件
    # 同键 → 第二个防重去重（existing_skipped），不被 A2 文件级校验拦截（9.mp4 仍被接受，
    # 只是同键去重）；此即「movie 全量保留原行为」。
    assert "已入队 1 个资源" in tr.message
    assert "1 个已有任务跳过" in tr.message
    assert "无关/超集号" not in tr.message  # movie 不套用 A2 过滤
    dq = run(_read_downloads(db, mid))
    assert [row.episode for row in dq] == ["movie:大话西游"]
    assert dq[0].status == "pending"
