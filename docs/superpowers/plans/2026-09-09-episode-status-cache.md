---
change: episode-status-cache
design-doc: docs/superpowers/specs/2026-09-09-episode-status-cache-design.md
base-ref: 93850d5355af929ea1d94da9a9231b204cd6b7ec
---

# episode-status-cache 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 或 subagent-driven-development 逐任务执行本计划。步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 修复影视列表/详情集数统计恒 0 与不全的问题；集信息落库缓存 + 每日刷新；统一标记状态机；单集真实大小；入库完成自动更新集数状态。

**Architecture:** 新增 `episode_info_cache` 表持久化 TMDB 集信息（复用现有 `get_tv_all_episodes` 回源），scheduler 注册每日刷新任务；routers/media.py 增加统一状态机 `resolve_episode_status` 并修正列表 total（TMDB 全集数优先）与详情合并视图；library_check finalize 联动双表；前端 format.ts 扩展状态映射并适配列表/详情展示。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.0（mapped_column 非注解风格）/ Alembic / APScheduler / Vue3 + Element Plus / Vitest

**Spec:** `docs/openspec/changes/episode-status-cache/specs/episode-cache/spec.md` + `specs/media-status/spec.md`；Design Doc `docs/superpowers/specs/2026-09-09-episode-status-cache-design.md`

## Global Constraints

- 语言：代码注释与新增文案使用中文；产物语言 zh-CN
- 模型风格：`mapped_column` 非注解风格（规避 SQLAlchemy 2.0.36 + Python 3.14 兼容缺陷，requirements.txt 已 pin，不可升级）
- 双后端：SQLite 与 PostgreSQL 双兼容（BIG_PK 用 `BigInteger().with_variant(Integer, "sqlite")`）
- 迁移：手写 alembic 版本（`backend/alembic/versions/`），`alembic upgrade head` 双后端可跑
- 脱敏：guest 不返回 share_code/aria2_gid/quark_path；admin 分享码仅后 4 位
- 降级铁律：TMDB/Emby 外部故障一律降级不抛异常，绝不阻断列表/详情接口
- 时间：后端统一 UTC naive（`now_utc_naive()`）；air_date 为 "YYYY-MM-DD" 字符串
- 集号：`SxxExx`（两位）；三位集号保留三位（S01E100）；解析用 `_parse_episode`（media.py）

---

### Task 1: episode_info_cache 表迁移 + ORM

**Files:**
- Create: `backend/alembic/versions/0015_episode_info_cache.py`
- Modify: `backend/app/models/__init__.py`（新增 `EpisodeInfoCache`）
- Test: `backend/tests/test_tmdb_cache.py`（追加）

**Interfaces:**
- Produces: `EpisodeInfoCache` ORM（字段 id/tmdb_id/season/episode/name/air_date/updated_at），供 Task 2/4 读写

- [x] **Step 1: 写迁移文件 `0015_episode_info_cache.py`**

参照 `backend/alembic/versions/0014_tmdb_cache_episode_count.py` 的写法：

```python
"""episode_info_cache: TMDB 集信息持久化缓存表（episode-status-cache 需求）

- tmdb_id + season + episode 唯一，按影视刷新、按集查询
- name/air_date 来自 TMDB season episodes（air_date 为 "YYYY-MM-DD" 或 NULL）
- 双后端（SQLite/PG）兼容：id 用 BigInteger+Identity（SQLite 退化为 INTEGER）
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_episode_info_cache"
down_revision = "0014_tmdb_cache_episode_count"

BIG_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "episode_info_cache",
        sa.Column("id", BIG_PK, sa.Identity(), primary_key=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("tmdb_id", "season", "episode", name="uq_episode_info_cache_tmdb_season_episode"),
    )


def downgrade() -> None:
    op.drop_table("episode_info_cache")
```

验证：`alembic upgrade head` 在 SQLite 临时库与 PG 均建表成功（先改文件，跑一次 upgrade 看无报错）。

- [x] **Step 2: models/__init__.py 新增 `EpisodeInfoCache`**

在 `TmdbCache` 类后追加（非注解风格，仿照既有类）：

```python
class EpisodeInfoCache(Base):
    """TMDB 集信息持久化缓存（episode-status-cache）：按影视 (tmdb_id) 缓存每季每集
    元数据（name / air_date），供影视详情与集数状态展示直接读取，避免实时 TMDB 查询。

    唯一键 (tmdb_id, season, episode)：支持按影视粒度刷新（逐集 upsert）。
    迁移见 alembic/versions/0015_episode_info_cache.py。
    """

    __tablename__ = "episode_info_cache"
    __table_args__ = (
        UniqueConstraint("tmdb_id", "season", "episode", name="uq_episode_info_cache_tmdb_season_episode"),
    )

    id = mapped_column(BIG_PK, Identity(), primary_key=True)
    tmdb_id = mapped_column(Integer, nullable=False)
    season = mapped_column(Integer, nullable=False)
    episode = mapped_column(Integer, nullable=False)
    name = mapped_column(Text, nullable=True)
    air_date = mapped_column(Text, nullable=True)
    updated_at = mapped_column(DateTime, server_default=text("CURRENT_TIMESTAMP"), onupdate=func.current_timestamp())
```

- [x] **Step 3: 跑迁移验证**

```bash
cd backend && alembic upgrade head 2>&1 | tail -5
```

Expected: 无报错；`alembic current` 显示 0015。随后 `pytest tests/test_api_smoke.py -q` 通过（app 能起、表元数据可加载）。

- [x] **Step 4: 提交**

```bash
git add backend/alembic/versions/0015_episode_info_cache.py backend/app/models/__init__.py
git commit -m "feat(episode-cache): 新增 episode_info_cache 集信息缓存表与 ORM"
```

---

### Task 2: 集信息回源与缓存读写函数

**Files:**
- Modify: `backend/app/services/tmdb.py`（新增 `refresh_episode_info` / `get_episode_info`）
- Test: `backend/tests/test_tmdb_cache.py`（追加）

**Interfaces:**
- Consumes: `get_tv_all_episodes(tmdb_id)`（既有，返回 [{season, episode, air_date, name}]）、`async_session`、`EpisodeInfoCache`（Task 1）
- Produces: `refresh_episode_info(tmdb_id: int) -> int`、`get_episode_info(tmdb_id: int) -> list[dict]`，供 Task 3/4 使用

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_tmdb_cache.py` 追加：

```python
# ---- episode_info_cache 集信息缓存（episode-status-cache） ----
# 测试模式与 test_library_check.py 一致：同步 def test_ + run() 包装 +
# in-memory SQLite（StaticPool）db fixture + monkeypatch tmdb_mod.async_session。
import asyncio

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  注册全部 ORM 模型
from app.models import EpisodeInfoCache  # noqa: F401


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


def _seed_episode_info(db, rows):
    async def _do():
        async with db() as s:
            for r in rows:
                s.add(EpisodeInfoCache(**r))
            await s.commit()
    run(_do())


def test_get_episode_info_empty_cache_returns_empty_list(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    result = run(tmdb_mod.get_episode_info(12345))
    assert result == []


def test_get_episode_info_returns_sorted_episodes(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    _seed_episode_info(db, [
        {"tmdb_id": 9, "season": 1, "episode": 2, "name": "Ep2", "air_date": "2026-01-02"},
        {"tmdb_id": 9, "season": 1, "episode": 1, "name": "Ep1", "air_date": "2026-01-01"},
    ])
    result = run(tmdb_mod.get_episode_info(9))
    assert [(r["season"], r["episode"]) for r in result] == [(1, 1), (1, 2)]
    assert result[0]["name"] == "Ep1"


def test_refresh_episode_info_upserts_and_counts(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    async def fake_all(tmdb_id):
        return [
            {"season": 1, "episode": 1, "air_date": "2026-01-01", "name": "A"},
            {"season": 1, "episode": 2, "air_date": "2026-01-08", "name": "B"},
        ]
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_all)
    n = run(tmdb_mod.refresh_episode_info(10))
    assert n == 2
    async def _count():
        async with db() as s:
            return len((await s.execute(select(EpisodeInfoCache).where(EpisodeInfoCache.tmdb_id == 10))).scalars().all())
    assert run(_count()) == 2


def test_refresh_episode_info_empty_preserves_old(db, monkeypatch):
    monkeypatch.setattr(tmdb_mod, "async_session", db)
    _seed_episode_info(db, [{"tmdb_id": 11, "season": 1, "episode": 1, "name": "Old", "air_date": "2026-01-01"}])
    async def fake_empty(tmdb_id):
        return []
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", fake_empty)
    n = run(tmdb_mod.refresh_episode_info(11))
    assert n == 0
    async def _count():
        async with db() as s:
            return len((await s.execute(select(EpisodeInfoCache).where(EpisodeInfoCache.tmdb_id == 11))).scalars().all())
    assert run(_count()) == 1
```

- [x] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/test_tmdb_cache.py -k "episode_info" -q 2>&1 | tail -8
```

Expected: FAIL（`AttributeError: module 'app.services.tmdb' has no attribute 'get_episode_info'`）。

- [x] **Step 3: 实现读写函数**

在 `backend/app/services/tmdb.py` 末尾（`get_tv_all_episodes` 之后）追加：

```python
# ---------------------------------------------------------------------------
# 集信息持久化缓存（episode-status-cache）：episode_info_cache 表读写
# ---------------------------------------------------------------------------
from sqlalchemy import delete as _sa_delete, select as _sa_select, update as _sa_update  # noqa: E402
from app.database import async_session as _async_session  # noqa: E402
from app.models import EpisodeInfoCache as _EpisodeInfoCache  # noqa: E402


async def refresh_episode_info(tmdb_id: int) -> int:
    """回源 TV 全部正片季每集信息并 upsert 到 episode_info_cache；返回写入行数。

    复用 get_tv_all_episodes（含 zh-CN / season 过滤 / 降级语义）：回源失败或空 → 返回 0，
    保留旧缓存（upsert 不动旧行）。仅供每日刷新任务 / 详情回源兜底调用。
    """
    episodes = await get_tv_all_episodes(tmdb_id)
    if not episodes:
        return 0
    count = 0
    async with _async_session() as s:
        async with s.begin():
            for ep in episodes:
                season = ep.get("season")
                episode = ep.get("episode")
                if season is None or episode is None:
                    continue
                row = await s.execute(
                    _sa_select(_EpisodeInfoCache).where(
                        _EpisodeInfoCache.tmdb_id == tmdb_id,
                        _EpisodeInfoCache.season == season,
                        _EpisodeInfoCache.episode == episode,
                    )
                )
                existing = row.scalar_one_or_none()
                if existing is None:
                    s.add(_EpisodeInfoCache(
                        tmdb_id=tmdb_id, season=season, episode=episode,
                        name=ep.get("name"), air_date=ep.get("air_date"),
                    ))
                else:
                    existing.name = ep.get("name")
                    existing.air_date = ep.get("air_date")
                count += 1
    return count


async def get_episode_info(tmdb_id: int) -> list[dict]:
    """读 episode_info_cache，返回 [{season, episode, name, air_date}] 按 season/episode 升序。

    无缓存返回 []（调用方回退回源或降级）。movie / 无 tmdb_id 场景由调用方控制不调用。
    """
    async with _async_session() as s:
        rows = (
            (await s.execute(
                _sa_select(_EpisodeInfoCache)
                .where(_EpisodeInfoCache.tmdb_id == tmdb_id)
                .order_by(_EpisodeInfoCache.season.asc(), _EpisodeInfoCache.episode.asc())
            ))
            .scalars()
            .all()
        )
    return [
        {"season": r.season, "episode": r.episode, "name": r.name, "air_date": r.air_date}
        for r in rows
    ]
```

- [x] **Step 4: 运行确认通过**

```bash
cd backend && pytest tests/test_tmdb_cache.py -k "episode_info" -q 2>&1 | tail -8
```

Expected: PASS（4 个用例全绿）。

- [x] **Step 5: 提交**

```bash
git add backend/app/services/tmdb.py backend/tests/test_tmdb_cache.py
git commit -m "feat(episode-cache): 集信息缓存读写 refresh_episode_info/get_episode_info"
```

---

### Task 3: 每日定时刷新任务注册

**Files:**
- Modify: `backend/app/scheduler.py`（JOB 常量 + register_jobs + import）
- Create: `backend/app/tasks/episode_info_refresh.py`
- Test: `backend/tests/test_scheduler.py`（追加）

**Interfaces:**
- Consumes: `refresh_episode_info`（Task 2）、`Media`、`async_session`、`get_job_enabled`
- Produces: `episode_info_refresh_job`（APScheduler job 包装），注册到 scheduler

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_scheduler.py` 追加：

```python
def test_episode_info_refresh_job_runs_for_tv_media(monkeypatch):
    from app.tasks import episode_info_refresh as eir
    from app.tasks.episode_info_refresh import episode_info_refresh_job

    calls = []
    async def fake_refresh(tmdb_id):
        calls.append(tmdb_id)
        return 1
    monkeypatch.setattr(eir, "refresh_episode_info", fake_refresh)
    # media 查询返回空 → 不调用（框架已建表）
    run(episode_info_refresh_job())
    assert calls == []  # 空库不刷
```

- [x] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/test_scheduler.py -k "episode_info_refresh" -q 2>&1 | tail -6
```

Expected: FAIL（module not found）。

- [x] **Step 3: 创建任务模块**

创建 `backend/app/tasks/episode_info_refresh.py`：

```python
"""每日刷新 TMDB 集信息缓存（episode-status-cache）。

遍历全部 tv media（tmdb_id 非空）调用 services.tmdb.refresh_episode_info，
单影视失败跳过并 log warning，不中断全量刷新。间隔由 system_config
episode_info_refresh_interval_hours（默认 24h）控制，job 开关沿用 scheduler 双层开关。
"""
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import Media
from app.services import tmdb

logger = logging.getLogger(__name__)


async def episode_info_refresh() -> int:
    """全量刷新集信息缓存；返回成功刷新的影视数。"""
    async with async_session() as s:
        rows = (
            (await s.execute(
                select(Media.id, Media.tmdb_id).where(
                    Media.media_type == "tv",
                    Media.tmdb_id.is_not(None),
                )
            ))
            .all()
        )
    ok = 0
    for _mid, tmdb_id in rows:
        try:
            n = await tmdb.refresh_episode_info(tmdb_id)
            if n > 0:
                ok += 1
        except Exception as exc:  # noqa: BLE001  单影视失败跳过，不影响其余
            logger.warning("[episode_info_refresh] media tmdb=%s 刷新失败跳过: %s", tmdb_id, exc)
    return ok


async def episode_info_refresh_job() -> None:
    """APScheduler job 包装（每日一次）：异常不外泄，不影响调度器其余 job。"""
    try:
        await episode_info_refresh()
    except Exception:  # noqa: BLE001
        logger.exception("[episode_info_refresh] 定时任务异常")
```

- [x] **Step 4: scheduler.py 注册 job**

在 `backend/app/scheduler.py`：
1. import 行加 `episode_info_refresh`：`from app.tasks import capacity_alert, cleanup, episode_info_refresh, library_check, ...`
2. 常量区新增：

```python
JOB_EPISODE_INFO_REFRESH = "episode_info_refresh"
```

并加入 `JOB_IDS` 列表。
3. `register_jobs()` 末尾（`prune_history` 之后）新增：

```python
    # episode-status-cache：每日刷新 TMDB 集信息缓存（避免实时查询缓慢）
    scheduler.add_job(
        episode_info_refresh.episode_info_refresh_job,
        IntervalTrigger(hours=24),
        id=JOB_EPISODE_INFO_REFRESH,
        paused=True,  # P3-4：注册即暂停（冷切换铁律），由 _apply_job_switches 恢复
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
```

4. 模块 docstring 的 job 表新增一行说明。

- [x] **Step 5: 运行确认通过**

```bash
cd backend && pytest tests/test_scheduler.py -k "episode_info_refresh" -q 2>&1 | tail -6
cd backend && python -c "from app.scheduler import JOB_IDS; assert 'episode_info_refresh' in JOB_IDS; print('job registered ok')"
```

Expected: PASS；`JOB_IDS` 含 episode_info_refresh。

- [x] **Step 6: 提交**

```bash
git add backend/app/tasks/episode_info_refresh.py backend/app/scheduler.py backend/tests/test_scheduler.py
git commit -m "feat(episode-cache): 每日定时刷新集信息缓存任务"
```

---

### Task 4: 标记状态机与列表/详情聚合修正

**Files:**
- Modify: `backend/app/routers/media.py`（`resolve_episode_status` + `list_media` total + `get_media_detail` 合并视图）
- Modify: `backend/app/utils.py`（若 `resolve_episode_status` 放 utils，供列表/详情复用；建议放 media.py 顶部或 utils）
- Test: `backend/tests/test_media_two_queue.py` / `test_media_episode_tags.py`（追加）

**Interfaces:**
- Consumes: `get_episode_info`（Task 2）、`_parse_episode`、`TmdbCache.number_of_episodes`、三表聚合
- Produces: `resolve_episode_status(local_status, in_emby, air_date, now) -> str`；详情 episode_state 新字段 `name`/归一 `state`；列表 `episode_stats.total` 用 TMDB 全集数

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_media_two_queue.py` 追加（或用新增 `test_episode_status.py`）：

```python
import pytest
from datetime import date
from app.routers.media import resolve_episode_status


@pytest.mark.parametrize("local,in_emby,air_date,today,expected", [
    ("done", False, "2026-01-01", date(2026, 9, 9), "in_library"),
    (None, True, None, date(2026, 9, 9), "in_library"),           # Emby 收录即已在库
    ("failed", False, "2026-01-01", date(2026, 9, 9), "error"),   # 开播但失败
    ("unmatched", False, "2026-01-01", date(2026, 9, 9), "error"),
    ("queued", False, "2026-01-01", date(2026, 9, 9), "scanning"),# 巡检/下载中
    ("downloading", False, "2026-01-01", date(2026, 9, 9), "scanning"),
    (None, False, "2027-01-01", date(2026, 9, 9), "not_aired"),   # 未开播
    (None, False, None, date(2026, 9, 9), "pending"),             # 待定
])
def test_resolve_episode_status(local, in_emby, air_date, today, expected):
    assert resolve_episode_status(local, in_emby, air_date, today) == expected


def test_resolve_episode_status_error_over_scanning():
    # 同一集既有 failed 又有 queued → 异常优先于巡检中
    assert resolve_episode_status("failed", False, "2026-01-01", date(2026, 9, 9)) == "error"
```

- [x] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/test_media_two_queue.py -k "resolve_episode_status" -q 2>&1 | tail -6
```

Expected: FAIL（`ImportError: cannot import name 'resolve_episode_status'`）。

- [x] **Step 3: 实现 `resolve_episode_status`**

在 `backend/app/routers/media.py` 顶部（`_parse_episode` 附近）追加：

```python
def resolve_episode_status(
    local_status: str | None,
    in_emby: bool,
    air_date: str | None,
    today: date,
) -> str:
    """单集归一标记状态（episode-status-cache 状态机，优先级从高到低）：

    - in_library：已在库（本地 done 或 Emby 收录）
    - error：异常（开播但失败 / 未搜到资源：failed/error/unmatched）
    - scanning：巡检中（queued/transferring/downloading/ready/probing 等执行中态）
    - not_aired：未开播（air_date 晚于今天）
    - pending：待定（无任何可判定信息）

    已在库 > 异常 > 巡检中 > 未开播 > 待定。异常优先于巡检中（同集 failed 记录
    比 queued 更能反映「需要关注」）。
    """
    if in_emby or local_status == "done":
        return "in_library"
    if local_status in ("failed", "error", "unmatched"):
        return "error"
    if local_status in (
        "queued", "transferring", "downloading", "transfer", "scrape",
        "library", "pending", "probing", "ready", "idle",
    ):
        return "scanning"
    if air_date:
        try:
            if date.fromisoformat(air_date) > today:
                return "not_aired"
        except ValueError:
            pass  # 非法日期 → 不判未开播，走 pending
    return "pending"
```

顶部需要 `from datetime import date`（若未导入）。

- [x] **Step 4: 修正列表 `list_media` 的 total**

在 `list_media` 中，`_stats(media_id)` 的 `total` 改为 TMDB 全集数优先：

```python
    # episode-status-cache：total 优先 TMDB 全集数（tmdb_cache.number_of_episodes），
    # 无 TMDB 数据时回退三表聚合去重数。一次 IN 查询避免 N+1。
    tmdb_totals: dict[int, int] = {}
    cache_rows = (
        await session.execute(
            select(TmdbCache.tmdb_id, TmdbCache.number_of_episodes)
            .where(TmdbCache.tmdb_id.in_([str(m.tmdb_id) for m in media_rows if m.tmdb_id]))
        )
    )
    for tmid, num in cache_rows:
        if num:
            tmdb_totals[int(tmid)] = int(num)
```

`_stats(media_id)` 内：

```python
    total = tmdb_totals.get(media_id_tmdb, ep["total"]) or ep["total"]
```

（`media_id_tmdb` 为该 media 的 tmdb_id；无 tmdb_id 或无缓存数据回退 `ep["total"]`。若 tmdb 全集数 < 聚合数则取 max 兜底，避免缺失。）实现时在返回的 `_stats` 中：

```python
        total = ep["total"]
        tmid = m.tmdb_id
        if tmid and tmid in tmdb_totals:
            total = max(total, tmdb_totals[tmid])
        return {
            "total": total,
            "done": ep["done"],
            "failed": ep["failed"],
            "in_progress": ep["in_progress"],
            "available": ep["done"],
            "downloaded": ep["done"],
            "missing": total - ep["done"],
        }
```

需在 `list_media` 的 media_rows 循环外查 tmdb_totals，`_stats` 闭包可访问。

- [x] **Step 5: 修正详情 `get_media_detail` 合并视图**

在 `get_media_detail` 中，把 `episode_state` 输出改为「TMDB 全集轴 + 本地状态合并」：

1. 全集轴数据源优先缓存：`tmdb_eps = await tmdb.get_episode_info(media.tmdb_id) or await tmdb.get_tv_all_episodes(media.tmdb_id)`（缓存优先，回源兜底）
2. 构造本地按 (season, episode) 索引：`local_by_key = {(season, ep_num): dq_row_or_es_row}`（沿用现有 episode_rows 逻辑）
3. 每集输出合并 DTO：

```python
    merged_episodes: list[dict] = []
    local_by_key: dict[tuple[int, int], object] = {}
    for r in episode_rows:
        s, e = _parse_episode(r.episode)
        if s is not None and e is not None:
            local_by_key[(s, e)] = r

    for ep in tmdb_eps:
        s = ep.get("season")
        e = ep.get("episode")
        if s is None or e is None:
            continue
        code = f"S{int(s):02d}E{int(e):02d}"
        in_emby = code in in_emby_codes
        air = ep.get("air_date")
        local = local_by_key.get((s, e))
        local_status = None
        file_size = None
        updated_at = None
        file_name = None
        if local is not None:
            local_status = getattr(local, "status", None) or getattr(local, "state", None)
            file_size = getattr(local, "file_size", None)
            updated_at = getattr(local, "updated_at", None)
            file_name = getattr(local, "file_name", None)
        merged_episodes.append({
            "episode": code,
            "season": s,
            "episode_number": e,
            "name": ep.get("name"),
            "air_date": air,
            "in_emby": in_emby,
            "state": resolve_episode_status(local_status, in_emby, air, date.today()),
            "status": resolve_episode_status(local_status, in_emby, air, date.today()),
            "file_size": file_size,
            "size_gb": round(file_size / _GB, 2) if file_size else None,
            "file_name": file_name,
            "updated_at": updated_at,
        })
```

返回中 `"episode_state": merged_episodes`（替代现有 episode_rows DTO 列表）。`tmdb_episodes` 顶层字段保留兼容（与原逻辑一致）。无 tmdb_eps 时回退现有 episode_rows DTO 列表（保留有记录集展示）。

- [x] **Step 6: 运行测试确认通过**

```bash
cd backend && pytest tests/test_media_two_queue.py tests/test_media_episode_tags.py tests/test_api_smoke.py -q 2>&1 | tail -8
```

Expected: PASS（新增 resolve 用例 + 既有 media 用例不回归）。

- [x] **Step 7: 提交**

```bash
git add backend/app/routers/media.py backend/tests/
git commit -m "feat(media-status): 统一标记状态机 resolve_episode_status 与列表/详情聚合修正"
```

---

### Task 5: 入库完成后集数状态联动

**Files:**
- Modify: `backend/app/tasks/library_check.py`（`_finalize_done` 双表同步）
- Test: `backend/tests/test_library_check.py`（追加）

**Interfaces:**
- Consumes: `_finalize_done`（既有）、`EpisodeState`、`_parse_episode`、`fmt_episode`
- Produces: finalize 时同步 `episode_state(state='done', file_size)` 双表一致；列表聚合自然 +1

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_library_check.py` 追加（同步风格，沿用既有 `db`/`env` fixture 与 `run()` 包装）：

```python
def test_finalize_done_syncs_episode_state(db, env, monkeypatch):
    """finalize 后 download_queue=done 且 episode_state 双表一致（state='done'）。"""
    from app.models import DownloadQueue, EpisodeState
    from sqlalchemy import select
    from app.tasks import library_check
    from app.tasks import transfer as transfer_mod
    from app.tasks.library_check import _finalize_done

    patch_db(monkeypatch, db)
    # media(downloading) + download_queue(status='library', file_size 有值)
    async def seed():
        async with db() as s:
            media = Media(title="测试剧", media_type="tv", tmdb_id=77, status="downloading")
            s.add(media)
            await s.commit()
            await s.refresh(media)
            dq = DownloadQueue(
                media_id=media.id, episode="S01E01", status="library",
                file_name="剧.S01E01.mkv", file_size=2 * 1024 ** 3, quark_path="/quark/a.mkv",
                node_started_at=_now(),
            )
            s.add(dq)
            await s.commit()
            return media.id, dq.id
    mid, dq_id = run(seed())

    async def do_finalize():
        await _finalize_done(dq_id, mid, "S01E01", "剧.S01E01.mkv", "/quark/a.mkv", transfer_mod)
    run(do_finalize())

    async def assert_state():
        async with db() as s:
            dq = (await s.execute(select(DownloadQueue).where(DownloadQueue.id == dq_id))).scalar_one()
            es = (await s.execute(select(EpisodeState).where(
                EpisodeState.media_id == mid, EpisodeState.episode == "S01E01",
            ))).scalar_one_or_none()
            return dq.status, es
    dq_status, es = run(assert_state())
    assert dq_status == "done"
    assert es is not None and es.state == "done"
    assert es.file_size == 2 * 1024 ** 3
```

（沿用 test_library_check.py 既有 mock 模式：`env` fixture 已 mock `_remove_quark_files`/notifier/spawn；`patch_db` 把 `async_session` 指向 in-memory db。若 `_finalize_done` 内部引用 `notifier`/`_spawn` 均已被 env fixture 替换。）

- [x] **Step 2: 运行确认失败**

```bash
cd backend && pytest tests/test_library_check.py -k "finalize" -q 2>&1 | tail -6
```

Expected: FAIL（episode_state 未同步）。

- [x] **Step 3: 实现双表同步**

在 `_finalize_done` 中，置 dq done 的同一事务内追加 episode_state upsert：

```python
            if r.rowcount == 0:
                return
            # episode-status-cache：同步 legacy episode_state（state='done' + file_size），
            # 保证旧表兼容与列表聚合一致；同 media+episode 已存在则更新不重复插。
            dq_row = (await s.execute(
                select(DownloadQueue).where(DownloadQueue.id == dq_id)
            )).scalar_one_or_none()
            if dq_row is not None:
                es = (
                    await s.execute(
                        select(EpisodeState).where(
                            EpisodeState.media_id == media_id,
                            EpisodeState.episode == episode,
                        )
                    )
                ).scalar_one_or_none()
                if es is None:
                    s.add(EpisodeState(
                        media_id=media_id, episode=episode,
                        state="done", file_size=dq_row.file_size,
                        file_name=dq_row.file_name,
                    ))
                else:
                    es.state = "done"
                    es.file_size = dq_row.file_size
                    es.file_name = dq_row.file_name
            # P3-6：该 media 无其他进行中集 → tracking
            await transfer_mod._sync_media_status(media_id, s)
```

需确保 `select`/`EpisodeState` 已 import（library_check.py 已有 `EpisodeState` 引用则复用）。

- [x] **Step 4: 运行确认通过**

```bash
cd backend && pytest tests/test_library_check.py -q 2>&1 | tail -8
```

Expected: PASS（新增用例 + 既有不回归）。

- [x] **Step 5: 提交**

```bash
git add backend/app/tasks/library_check.py backend/tests/test_library_check.py
git commit -m "fix(media-status): 入库完成同步 episode_state 双表，集数统计联动"
```

---

### Task 6: 前端状态映射与展示修正

**Files:**
- Modify: `frontend/src/utils/format.ts`（归一状态映射 + episodeText 相关）
- Modify: `frontend/src/views/MediaListView.vue`（episodeText 展示真实统计）
- Modify: `frontend/src/views/MediaDetailView.vue`（集数状态行使用归一状态与真实大小）
- Test: `frontend/src/utils/format.test.ts`（新建，仿照 poster.test.ts）

**Interfaces:**
- Consumes: 后端 `episode_stats.total/available`、详情 `episode_state[].state/name/file_size`
- Produces: `EPISODE_STATUS_MAP`（in_library/error/scanning/not_aired/pending → 文案/颜色）；`episodeStatusLabel/Type`

- [x] **Step 1: 写失败测试**

创建 `frontend/src/utils/format.test.ts`：

```ts
import { describe, it, expect } from 'vitest'
import {
  episodeStatusLabel,
  episodeStatusType,
  formatBytes,
} from './format'

describe('归一集数状态映射（episode-status-cache）', () => {
  it('覆盖五状态文案', () => {
    expect(episodeStatusLabel('in_library')).toBe('已在库')
    expect(episodeStatusLabel('error')).toBe('异常')
    expect(episodeStatusLabel('scanning')).toBe('巡检中')
    expect(episodeStatusLabel('not_aired')).toBe('未开播')
    expect(episodeStatusLabel('pending')).toBe('待定')
  })
  it('未知状态回退原文', () => {
    expect(episodeStatusLabel('whatever')).toBe('whatever')
  })
  it('formatBytes 格式化真实大小', () => {
    expect(formatBytes(2 * 1024 ** 3)).toBe('2.00 GB')
    expect(formatBytes(null)).toBe('—')
  })
})
```

- [x] **Step 2: 运行确认失败**

```bash
cd frontend && npx vitest run src/utils/format.test.ts 2>&1 | tail -8
```

Expected: FAIL（`episodeStatusLabel` 未定义）。

- [x] **Step 3: 实现 format.ts 映射**

在 `frontend/src/utils/format.ts` 追加：

```ts
/** 归一集数状态（后端 resolve_episode_status）→ [中文标签, tag type] */
const EPISODE_STATUS_MAP: Record<string, [string, string]> = {
  in_library: ['已在库', 'success'],
  error: ['异常', 'danger'],
  scanning: ['巡检中', 'warning'],
  not_aired: ['未开播', 'info'],
  pending: ['待定', 'info'],
}

export function episodeStatusLabel(status: string | null | undefined): string {
  if (!status) return '—'
  return EPISODE_STATUS_MAP[status]?.[0] ?? status
}

export function episodeStatusType(status: string | null | undefined): string {
  if (!status) return 'info'
  return EPISODE_STATUS_MAP[status]?.[1] ?? 'info'
}
```

- [x] **Step 4: 适配 MediaListView episodeText**

`MediaListView.vue` 的 `episodeText` 已使用 `s.available ?? s.downloaded` 与 `s.total`——后端数值修正后自动正确；无需改结构。仅确认电影分支与无统计回退保持。如需要把「未开播/待定」集计入 total 的语义澄清，保持现状（total 来自后端）。

- [x] **Step 5: 适配 MediaDetailView 集数状态行**

`MediaDetailView.vue`：
1. `episodeRows` computed 已对每行做 `episodeStateTag` —— 归一状态来自后端 `state` 字段。若后端 `state` 为 `in_library/error/scanning/not_aired/pending`，前端 `episodeStateTag` 需兼容这些新状态（在后端未返回 `in_emby` 时靠 `state` 判定）。追加 fallback：

```ts
function episodeStateTag(row: Record<string, unknown>, today: Date = new Date()): EpisodeStateTag {
  // episode-status-cache：优先使用后端归一状态（in_library/error/scanning/not_aired/pending）
  const norm = typeof row.state === 'string' ? row.state : ''
  if (norm === 'in_library') return { label: '已在库', type: 'success', reason: '该集已在库' }
  if (norm === 'error') return { label: '异常', type: 'danger', reason: '开播但未下载成功（含未搜到资源）' }
  if (norm === 'scanning') return { label: '巡检中', type: 'warning', reason: '正在巡检/下载中' }
  if (norm === 'not_aired') return { label: '未开播', type: 'info', reason: '尚未开播' }
  if (norm === 'pending') return { label: '待定', type: 'info', reason: '暂无判定信息' }
  // 回退既有启发式判定
  ...
}
```

2. 集数状态表格的「大小」列展示 `row.size_gb`（已有 `formatGb`）——后端已改逐行真实值，缺失时前端显示 '—'。确认表格「集」列用 `episodeLabel(row)`（SxxExx），新增「名称」列展示 `row.name`（若有）。

- [x] **Step 6: 运行确认通过**

```bash
cd frontend && npx vitest run src/utils/format.test.ts 2>&1 | tail -6
cd frontend && npm run build 2>&1 | tail -6
```

Expected: PASS；build 通过（vue-tsc 无类型错误）。

- [x] **Step 7: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/utils/format.test.ts frontend/src/views/MediaListView.vue frontend/src/views/MediaDetailView.vue
git commit -m "feat(media-status): 前端归一集数状态映射与列表/详情展示适配"
```

---

### Task 7: 全量回归验证

**Files:**
- 无新文件；全量跑既有测试

- [ ] **Step 1: 后端全量测试**

```bash
cd backend && python -m pytest -q 2>&1 | tail -10
```

Expected: 全部通过（无回归；新增 episode_info / resolve / finalize 用例绿）。

- [ ] **Step 2: 前端全量测试 + 构建**

```bash
cd frontend && npx vitest run 2>&1 | tail -6
cd frontend && npm run build 2>&1 | tail -6
```

Expected: vitest 全绿；build 通过。

- [ ] **Step 3: 收尾提交（若 Task 1-6 有遗漏未提交）**

```bash
git status --short
git add -A && git commit -m "chore(episode-status-cache): 全量回归与收尾"
```

（若没有未提交改动则跳过本步。）

- [ ] **Step 4: 勾选 tasks.md 全部任务并记录验证**

确认 `docs/openspec/changes/episode-status-cache/tasks.md` 全部勾选；在 build 记录真实构建证据。
