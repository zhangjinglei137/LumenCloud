---
change: episode-status-and-detail-polish
design-doc: docs/superpowers/specs/2026-09-11-episode-status-and-detail-polish-design.md
base-ref: 08761cb6a5b02943f14fc277849220ea52aa345a
---

# episode-status-and-detail-polish 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 列表「已有 N 缺失 M」统计加入 Emby 实际入库维度（去重合并）；详情页新增「当前进行中任务」区块；修复详情页「状态」下拉宽度。

**Architecture:** 后端 `emby.py` 新增带进程内 TTL 缓存的已入库集查询（`get_ingested_episode_codes`，配置指纹做缓存 key）；`list_media._stats` 仅对 tv + 有缺失的影视并发触发 Emby 聚合，`available = len(本系统完成集 ∪ Emby 入库集)`；`get_media` 新增 `active_tasks` 字段（download_queue ∪ task_queue 进行中行，过滤已入库/未到首播日，带 source 标识）。前端复用既有状态字典，新增紧凑表格区块。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / pytest；Vue 3 / TypeScript / Element Plus / Vitest。

**Spec:** `docs/openspec/changes/episode-status-and-detail-polish/specs/media-status/spec.md` + `specs/media-detail-ui/spec.md`（需求契约事实源）；设计细化见 `docs/superpowers/specs/2026-09-11-episode-status-and-detail-polish-design.md`。

## Global Constraints

- 产物语言：zh-CN（所有注释、提交信息、测试描述用中文）
- 状态文案复用既有字典（DOWNLOAD_QUEUE_STATUS_MAP / TASK_QUEUE_STATUS_MAP），不新建状态定义
- Emby 外部服务故障一律降级（列表 available 回退 done、详情 in_emby 全 False），接口绝不报错
- `missing = max(0, total - available)`，防 TMDB 全集数滞后产生负值
- 不改变下载队列/任务队列的流转逻辑，本 change 只做展示
- 每次任务完成跑对应测试并提交（Conventional Commits 结构 + 中文描述）

---

## Task 1: emby.py 新增 `get_ingested_episode_codes`（含进程内 TTL 缓存）

对应 tasks.md：1.1 + 1.2

**Files:**
- Modify: `backend/app/services/emby.py`（顶部 import 区 + 新函数，放在 `list_episodes` 之后）
- Test: `backend/tests/test_ingested_episodes.py`（新建）

**Interfaces:**
- Consumes: `find_emby_id(tmdb_id, title)`（emby.py:232）、`list_episodes(emby_id)`（emby.py:326）、`config_store.get`、`settings.EMBY_BASE_URL/EMBY_API_KEY`
- Produces: `get_ingested_episode_codes(tmdb_id: int, title: str | None = None) -> set[str]`（已入库集 "SxxExx" 集合；模块级缓存 `_INGESTED_CACHE`，TTL 6h，缓存 key 含配置指纹，配置变化自然失效；`EmbyUnavailable` 不缓存、上抛）

- [x] **Step 1: 写失败测试**

创建 `backend/tests/test_ingested_episodes.py`：

```python
"""emby.get_ingested_episode_codes 单测：已入库集聚合 + 进程内 TTL 缓存。

覆盖：
- 命中缓存：mock 服务端一次调用，二次调用零网络（find_emby_id/list_episodes 不再被调）
- 配置变化失效：config_store 值变化 → 缓存 key 变化 → 重新查询
- find_emby_id 未命中（影视不在 Emby）→ 空集且缓存（二次调用零网络）
- EmbyUnavailable 上抛（不缓存）
"""
from unittest.mock import AsyncMock

import pytest

from app.services import emby as emby_mod


@pytest.fixture(autouse=True)
def _clear_ingested_cache():
    emby_mod._INGESTED_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()


@pytest.mark.asyncio
async def test_hit_returns_cached_set_without_network(monkeypatch):
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[
        {"code": "S01E01"}, {"code": "S01E03"},
    ]))

    first = await emby_mod.get_ingested_episode_codes(1001, "剧名")
    assert first == {"S01E01", "S01E03"}
    assert emby_mod.find_emby_id.await_count == 1

    second = await emby_mod.get_ingested_episode_codes(1001, "剧名")
    assert second == {"S01E01", "S01E03"}
    assert emby_mod.find_emby_id.await_count == 1  # 二次调用零网络


@pytest.mark.asyncio
async def test_config_change_invalidates_cache(monkeypatch):
    from app.services import config_store

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[{"code": "S01E01"}]))
    # 先取一次（配置为空时 find_emby_id 直接抛 EmbyUnavailable，但被 mock 短路）
    await emby_mod.get_ingested_episode_codes(1002, "剧名")

    # 模拟配置变化（base_url 变化 → 指纹变化）
    monkeypatch.setattr(config_store, "get", lambda key, default=None: {
        "emby_base_url": "http://new-emby:8096",
        "emby_api_key": "new-key",
    }.get(key, default))

    await emby_mod.get_ingested_episode_codes(1002, "剧名")
    assert emby_mod.find_emby_id.await_count == 2  # 配置变化后重新查询


@pytest.mark.asyncio
async def test_not_in_emby_caches_empty(monkeypatch):
    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=None))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=[{"code": "S01E01"}]))

    assert await emby_mod.get_ingested_episode_codes(1003, "剧名") == set()
    assert await emby_mod.get_ingested_episode_codes(1003, "剧名") == set()
    assert emby_mod.find_emby_id.await_count == 1  # 空集同样缓存


@pytest.mark.asyncio
async def test_emby_unavailable_not_cached(monkeypatch):
    from app.services.emby import EmbyUnavailable

    async def boom(*_a, **_k):
        raise EmbyUnavailable("EMBY_BASE_URL 未配置")

    monkeypatch.setattr(emby_mod, "find_emby_id", boom)
    with pytest.raises(EmbyUnavailable):
        await emby_mod.get_ingested_episode_codes(1004, "剧名")
    # 失败不缓存 → 再次调用仍会重试（find_emby_id 被再次调用）
    with pytest.raises(EmbyUnavailable):
        await emby_mod.get_ingested_episode_codes(1004, "剧名")
    assert emby_mod.find_emby_id.await_count == 2
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_ingested_episodes.py -v`
Expected: FAIL（`_INGESTED_CACHE` 不存在 / `get_ingested_episode_codes` 未定义）

- [x] **Step 3: 实现 `get_ingested_episode_codes`**

在 `backend/app/services/emby.py` 中：

a) 顶部 import 区新增 `import hashlib`（放在现有 `import asyncio` 附近）：

```python
import asyncio
import hashlib
import logging
```

b) 在模块常量区（`_LIST_MAX_PAGES` 之后）新增：

```python
# episode-status-and-detail-polish：Emby 已入库集聚合的进程内 TTL 缓存。
# 模式对齐 tmdb.py _SEASON_AIR_CACHE（模块级 dict + timestamp + TTL）。
_INGESTED_TTL_SECONDS = 6 * 3600  # 6h，与 TMDB season air_date 缓存一致
_INGESTED_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
```

c) 在 `list_episodes` 函数之后新增：

```python
def _config_fingerprint() -> str:
    """Emby 配置指纹：base_url + api_key 的短哈希（缓存 key 前缀）。

    配置变化（设置页 PATCH /api/settings）→ 指纹变化 → 缓存 key 变化 →
    旧缓存自然失效，无需主动清理（tasks 1.2 验收点）。
    """
    raw = (
        f"{config_store.get('emby_base_url', settings.EMBY_BASE_URL)}|"
        f"{config_store.get('emby_api_key', settings.EMBY_API_KEY)}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


async def get_ingested_episode_codes(tmdb_id: int, title: Optional[str] = None) -> set[str]:
    """查询影视已入库集号集合（"SxxExx"），带进程内 TTL 缓存（6h）。

    组合 find_emby_id（tmdb_id → Emby Item Id，含 title 模糊兜底）+ list_episodes
    （已入库集列表）→ 归一 code 集合。供列表「已有 N 缺失 M」统计与详情
    active_tasks「已入库剔除」复用。

    缓存语义：
    - key = 配置指纹 + tmdb_id（配置变化自然失效）
    - find_emby_id 未命中（影视不在 Emby）→ 缓存空集（TTL 内避免反复查询）
    - EmbyUnavailable（配置缺失/网络故障）→ 不缓存、上抛，由调用方降级

    参数:
        tmdb_id: TMDB id
        title:   影视名称（可选；find_emby_id 模糊兜底用）
    返回:
        已入库集 code 集合（可能为空集）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    key = f"{_config_fingerprint()}:{tmdb_id}"
    hit = _INGESTED_CACHE.get(key)
    if hit is not None and datetime.now(timezone.utc).timestamp() - hit[0] < _INGESTED_TTL_SECONDS:
        return set(hit[1])

    emby_id = await find_emby_id(tmdb_id, title)
    codes: set[str] = set()
    if emby_id:
        eps = await list_episodes(emby_id)
        codes = {str(ep.get("code")) for ep in eps if ep.get("code")}
    _INGESTED_CACHE[key] = (datetime.now(timezone.utc).timestamp(), frozenset(codes))
    return codes
```

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_ingested_episodes.py -v`
Expected: 4 个测试全部 PASS

- [x] **Step 5: 全量后端测试回归**

Run: `cd backend && python -m pytest -q`
Expected: 全部通过（无既有测试受影响）

- [x] **Step 6: 提交**

```bash
git add backend/app/services/emby.py backend/tests/test_ingested_episodes.py
git commit -m "feat(emby): 新增已入库集查询 get_ingested_episode_codes（TTL 缓存 + 配置指纹失效）"
```

---

## Task 2: media.py 列表 `_stats` 扩展 Emby 入库维度

对应 tasks.md：1.3

**Files:**
- Modify: `backend/app/routers/media.py`（顶部 import + `list_media` 内聚合区 + `_stats`）
- Test: `backend/tests/test_media_list_emby_stats.py`（新建）

**Interfaces:**
- Consumes: `emby.get_ingested_episode_codes(tmdb_id, title)`（Task 1 产物）
- Produces: `episode_stats.available`（升级为 Emby 入库 ∪ 本系统完成 去重计数）、`episode_stats.missing = max(0, total - available)`

- [x] **Step 1: 写失败测试**

创建 `backend/tests/test_media_list_emby_stats.py`（复用 test_media_episode_tags.py 的 seed/登录模式）：

```python
"""列表 _stats「已有 N 缺失 M」Emby 维度单测。

覆盖：
- 已有口径：Emby 入库 ∪ 本系统完成 去重合并
- Emby 故障（EmbyUnavailable）→ available 回退 done，接口 200
- missing 不为负（Emby 已有集数 > TMDB 全集数）
- 仅对 tv + 有缺失的影视触发 Emby 查询（total==done 时不调用）
"""
import itertools
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_list_emby_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.main import app  # noqa: E402

_TMDB_ID = itertools.count(2000)


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
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return _auth(r.json()["access_token"])


async def _seed_tv_with_done(total_episodes: int):
    """tv + tmdb_id：tmdb_cache 全集数 total_episodes，本系统已完成 S01E01（1 集）。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media, TmdbCache

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    tmid = next(_TMDB_ID)
    async with async_session() as session:
        media = Media(title="测试剧", media_type="tv", tmdb_id=tmid, status="tracking")
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1024**3, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        session.add(TmdbCache(tmdb_id=str(tmid), media_type="tv",
                              number_of_episodes=total_episodes, payload="{}"))
        await session.commit()
        return {"media_id": media.id}


@pytest.fixture(autouse=True)
def _clear_ingested_cache():
    from app.services import emby as emby_mod

    emby_mod._INGESTED_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()


def test_stats_union_emby_and_done(monkeypatch):
    """tv：Emby 入库 3 集（S01E02/03/04，无本地记录）+ 本系统完成 1 集（S01E01）→
    available=4，missing = 20-4 = 16。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        "S01E02", "S01E03", "S01E04"}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done(20))["media_id"]
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 4
        assert item["episode_stats"]["missing"] == 16


def test_stats_dedup_overlap(monkeypatch):
    """Emby 入库与本地完成同集（S01E01）→ 去重后 available=1 而非 2。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        "S01E01"}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done(20))["media_id"]
        r = client.get("/api/media", headers=h)
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        assert item["episode_stats"]["missing"] == 19


def test_stats_emby_failure_degrades(monkeypatch):
    """Emby 故障（抛 EmbyUnavailable）→ available 回退 done（1），接口 200。"""
    from app.services import emby as emby_mod
    from app.services.emby import EmbyUnavailable

    async def boom(*_a, **_k):
        raise EmbyUnavailable("emby down")

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", boom)

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done(20))["media_id"]
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        assert item["episode_stats"]["missing"] == 19


def test_stats_missing_not_negative(monkeypatch):
    """Emby 已有集数（25）> TMDB 全集数（20）→ missing=0 而非 -5。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", AsyncMock(return_value={
        f"S01E{i:02d}" for i in range(1, 26)}))

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done(20))["media_id"]
        r = client.get("/api/media", headers=h)
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["missing"] == 0


def test_stats_no_emby_query_when_no_missing(monkeypatch):
    """本系统已完成 == 全集数（无缺失）→ 不触发 Emby 查询。"""
    from app.services import emby as emby_mod

    mock = AsyncMock(return_value=set())
    monkeypatch.setattr(emby_mod, "get_ingested_episode_codes", mock)

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_with_done(1))["media_id"]  # done=1, total=1
        r = client.get("/api/media", headers=h)
        assert r.status_code == 200, r.text
        item = next(m for m in r.json() if m["id"] == mid)
        assert item["episode_stats"]["available"] == 1
        mock.assert_not_awaited()
```

注意：`TmdbCache` 模型字段需与 `list_media` 中 `select(TmdbCache.tmdb_id, TmdbCache.number_of_episodes)` 对齐（tmdb_id 为字符串、number_of_episodes 为 int）。

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_media_list_emby_stats.py -v`
Expected: FAIL（`available` 仍等于 done=1，断言 4 不满足；Emby 维度的用例失败）

- [x] **Step 3: 实现 `_stats` Emby 维度**

在 `backend/app/routers/media.py` 中：

a) 顶部 import 区新增（现有 import 位置）：

```python
import asyncio

from app.services import emby  # 若尚未 import，检查现有 `from app.services.emby import ...`
```

先检查 media.py 顶部是否已 import `emby`（详情接口 `get_media` 已用 `emby.find_emby_id`，应已有）；若已有则跳过。

b) 在 `counts` 聚合完成后（`for media_id, ep_map in _rank.items():` 循环之后、`latest` 查询之前）插入：

```python
    # episode-status-and-detail-polish：「已有」口径升级为 Emby 入库 ∪ 本系统完成（去重）。
    # 仅对 tv + tmdb_id 存在 + 有缺失（total > done）的影视触发 Emby 聚合；
    # asyncio.gather 并发（TTL 缓存命中时零网络开销）；任一部失败降级为空集。
    done_codes_by_media: dict[int, set[str]] = {
        media_id: {ep for ep, rank in ep_map.items() if rank == 1}
        for media_id, ep_map in _rank.items()
    }
    ingested_by_media: dict[int, set[str]] = {}

    async def _fetch_ingested(m: Media) -> tuple[int, set[str]]:
        try:
            return m.id, await emby.get_ingested_episode_codes(m.tmdb_id, m.title)
        except Exception as exc:  # noqa: BLE001  含 EmbyUnavailable：降级为空集，不阻断列表
            logger.warning("[media] list Emby 已入库集查询降级 media=%s: %s", m.id, exc)
            return m.id, set()

    emby_targets = [
        m for m in media_rows
        if (m.media_type or "").strip().lower() != "movie" and m.tmdb_id is not None
        and (tmdb_totals.get(m.tmdb_id) or 0) > len(done_codes_by_media.get(m.id, set()))
    ]
    if emby_targets:
        results = await asyncio.gather(*(_fetch_ingested(m) for m in emby_targets))
        ingested_by_media = dict(results)
```

c) 修改 `_stats`：

```python
    def _stats(m: Media) -> dict:
        ep = counts.get(m.id, {"total": 0, "done": 0, "failed": 0, "in_progress": 0})
        total = ep["total"]
        tmid = m.tmdb_id
        if tmid and tmid in tmdb_totals:
            total = max(total, tmdb_totals[tmid])
        done_codes = done_codes_by_media.get(m.id, set())
        ingested = ingested_by_media.get(m.id, set())
        available = len(done_codes | ingested)
        return {
            "total": total,
            "done": ep["done"],
            "failed": ep["failed"],
            "in_progress": ep["in_progress"],
            # 前端契约别名（§8 影视列表「已有/总集数」）：
            # 「已有」= Emby 实际入库 ∪ 本系统完成（去重）；缺失防负
            "available": available,
            "downloaded": ep["done"],
            "missing": max(0, total - available),
        }
```

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_media_list_emby_stats.py -v`
Expected: 5 个测试全部 PASS

- [x] **Step 5: 全量后端测试回归**

Run: `cd backend && python -m pytest -q`
Expected: 全部通过（既有列表/详情测试不受影响）

- [x] **Step 6: 提交**

```bash
git add backend/app/routers/media.py backend/tests/test_media_list_emby_stats.py
git commit -m "feat(media): 列表 _stats「已有」加入 Emby 入库维度（去重合并 + 缺失防负 + 故障降级）"
```

---

## Task 3: media.py 详情接口新增 `active_tasks` 字段

对应 tasks.md：1.4

**Files:**
- Modify: `backend/app/routers/media.py`（`get_media` 内，`in_emby_codes`/`tmdb_episodes` 构建完成后）
- Test: `backend/tests/test_media_active_tasks.py`（新建）

**Interfaces:**
- Consumes: `_DQ_ACTIVE_STATUSES` / `_TQ_ACTIVE_STATUSES`（media.py:44-45）、`_parse_episode`（media.py:121）、`in_emby_codes`、`tmdb_episodes`（均在 get_media 内已有）
- Produces: 响应新增可选字段 `active_tasks: [{season, episode, status, source: "dq"|"task", air_date}]`（仅 tv，按 (season, episode) 升序；movie 不返回）

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_media_active_tasks.py`：

```python
"""详情接口 active_tasks 字段单测（「当前进行中任务」数据源）。

覆盖：
- download_queue 进行中 + task_queue 进行中合并，source/status 透传
- 终态剔除（done/failed/skipped 不出现）
- 已入库剔除（code 命中 in_emby_codes）
- 未到首播日剔除（air_date 未来）；air_date 未知保留
- 排序 (season, episode) 升序
- movie 不返回该字段
"""
import itertools
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_active_tasks_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.main import app  # noqa: E402

_TMDB_ID = itertools.count(3000)


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
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return _auth(r.json()["access_token"])


async def _seed_tv_mixed_queue():
    """tv：dq 行 S01E01 downloading（进行中）、S01E02 done（终态）、
    tq 行 S01E03 probing（进行中）、S01E04 ready（进行中）。"""
    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="测试剧", media_type="tv", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.flush()
        for ep, status in (("S01E01", "downloading"), ("S01E02", "done")):
            session.add(DownloadQueue(
                media_id=media.id, episode=ep, file_name=f"{ep}.mkv",
                file_size=1024**3, share_code="ScAa1111", status=status,
                enqueued_at=now, updated_at=now,
            ))
        for ep, status in (("S01E03", "probing"), ("S01E04", "ready")):
            session.add(TaskQueue(
                media_id=media.id, episode=ep, status=status,
                created_at=now, updated_at=now,
            ))
        await session.commit()
        return {"media_id": media.id}


async def _seed_movie():
    from app.database import async_session
    from app.models import Media

    async with async_session() as session:
        media = Media(title="电影", media_type="movie", tmdb_id=next(_TMDB_ID), status="tracking")
        session.add(media)
        await session.commit()
        return {"media_id": media.id}


@pytest.fixture(autouse=True)
def _clear_caches():
    from app.services import emby as emby_mod
    from app.services import tmdb as tmdb_mod

    emby_mod._INGESTED_CACHE.clear()
    tmdb_mod._SEASON_AIR_CACHE.clear()
    yield
    emby_mod._INGESTED_CACHE.clear()
    tmdb_mod._SEASON_AIR_CACHE.clear()


def _mock_emby(monkeypatch, emby_id="e1", episodes=None):
    """mock Emby：find_emby_id 命中 e1；已入库集 episodes（默认空=全部未入库）。"""
    from app.services import emby as emby_mod

    monkeypatch.setattr(emby_mod, "find_emby_id", AsyncMock(return_value=emby_id))
    monkeypatch.setattr(emby_mod, "list_episodes", AsyncMock(return_value=episodes or []))


def test_active_tasks_merge_and_source(monkeypatch):
    """dq + tq 进行中合并；终态 done 剔除；source 区分 dq/task。"""
    _mock_emby(monkeypatch)
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        tasks = r.json()["active_tasks"]
        by_ep = {t["episode"]: t for t in tasks}
        assert "S01E02" not in by_ep  # done 终态剔除
        assert by_ep["S01E01"]["source"] == "dq"
        assert by_ep["S01E01"]["status"] == "downloading"
        assert by_ep["S01E03"]["source"] == "task"
        assert by_ep["S01E03"]["status"] == "probing"
        assert by_ep["S01E04"]["source"] == "task"
        assert by_ep["S01E04"]["status"] == "ready"


def test_active_tasks_excludes_ingested(monkeypatch):
    """已入库集（code 命中 Emby）剔除：S01E01 在 Emby → 不出现；S01E03 不在 → 出现。"""
    _mock_emby(monkeypatch, episodes=[{"code": "S01E01"}])
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        tasks = {t["episode"] for t in r.json()["active_tasks"]}
        assert "S01E01" not in tasks
        assert "S01E03" in tasks


def test_active_tasks_excludes_not_aired(monkeypatch):
    """未到首播日剔除：S01E03 air_date 未来 → 剔除；air_date 缺失 → 保留。"""
    from app.services import tmdb as tmdb_mod
    from app.services.tmdb import get_tv_all_episodes

    _mock_emby(monkeypatch)
    # tmdb_episodes 全集轴：S01E03 air_date 未来；S01E04 无 air_date
    monkeypatch.setattr(tmdb_mod, "get_episode_info", AsyncMock(return_value=[
        {"season": 1, "episode": 3, "air_date": "2099-01-01", "name": "E03"},
        {"season": 1, "episode": 4, "name": "E04"},
    ]))
    monkeypatch.setattr(tmdb_mod, "get_tv_all_episodes", AsyncMock(
        side_effect=get_tv_all_episodes))  # 兜底不被调用

    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_tv_mixed_queue)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        tasks = {t["episode"] for t in r.json()["active_tasks"]}
        assert "S01E03" not in tasks  # 未到首播日
        assert "S01E04" in tasks  # air_date 未知保留


def test_active_tasks_movie_absent(monkeypatch):
    """movie 详情不返回 active_tasks 字段。"""
    _mock_emby(monkeypatch)
    with TestClient(app) as client:
        h = _login(client)
        mid = client.portal.call(_seed_movie)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert "active_tasks" not in r.json()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_media_active_tasks.py -v`
Expected: FAIL（`active_tasks` 键不存在）

- [ ] **Step 3: 实现 `active_tasks` 构建**

在 `backend/app/routers/media.py` 的 `get_media` 中，`merged_episodes` 构建之后、`tq_rows` 查询之前（或 `tq_rows` 之后均可，需 `tq_rows` 在作用域内）插入：

```python
    # episode-status-and-detail-polish：「当前进行中任务」数据（仅 tv）。
    # 合并 download_queue（执行层）+ task_queue（探测层）进行中行；过滤：
    # 1) 已入库（code 命中 in_emby_codes，Emby 故障时空集 → 全部保留，宽松）；
    # 2) 未到首播日（air_date 已知且未来；缺失/非法 → 保留）。
    # source 标识供前端选状态字典（task「待探测」vs dq「排队中」语义不同）。
    active_tasks: list[dict] = []
    if (media.media_type or "").strip().lower() != "movie":
        air_by_key: dict[tuple[int, int], str | None] = {
            (int(ep["season"]), int(ep["episode"])): ep.get("air_date")
            for ep in (tmdb_episodes or [])
            if ep.get("season") is not None and ep.get("episode") is not None
        }
        today = date.today()
        raw: list[tuple[int | None, int | None, str, str, str]] = []
        for r in dq_rows:
            if r.status in _DQ_ACTIVE_STATUSES:
                s, e = _parse_episode(r.episode)
                raw.append((s, e, r.episode, r.status, "dq"))
        for r in tq_rows:
            if r.status in _TQ_ACTIVE_STATUSES:
                s, e = _parse_episode(r.episode)
                raw.append((s, e, r.episode, r.status, "task"))

        seen: set[tuple[int | None, int | None, str]] = set()
        for s, e, episode, status, source in raw:
            code = episode
            if s is not None and e is not None:
                code = f"S{int(s):02d}E{int(e):02d}"
            if code in in_emby_codes:
                continue  # 已入库，不展示
            air = air_by_key.get((s, e)) if s is not None and e is not None else None
            if air:
                try:
                    if date.fromisoformat(air) > today:
                        continue  # 未到首播日
                except ValueError:
                    pass  # 非法日期不剔除
            key = (s, e, source)
            if key in seen:
                continue  # 同集同源去重（dq 行可能重复）
            seen.add(key)
            active_tasks.append({
                "season": s,
                "episode": code,
                "status": status,
                "source": source,
                "air_date": air,
            })
        active_tasks.sort(key=lambda t: (
            t["season"] is None, t["season"] or 0,
            _parse_episode(t["episode"])[1] if t["season"] is not None else 0,
        ))
```

在响应组装（return 前）追加：

```python
    if (media.media_type or "").strip().lower() != "movie":
        media_dto["active_tasks"] = active_tasks
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_media_active_tasks.py -v`
Expected: 5 个测试全部 PASS

- [ ] **Step 5: 全量后端测试回归**

Run: `cd backend && python -m pytest -q`
Expected: 全部通过

- [ ] **Step 6: 提交**

```bash
git add backend/app/routers/media.py backend/tests/test_media_active_tasks.py
git commit -m "feat(media): 详情接口新增 active_tasks 字段（进行中任务合并、已入库/未开播过滤、source 标识）"
```

---

## Task 4: 后端测试总验证（勾选 1.5）

对应 tasks.md：1.5

**Files:** 无新增（1.1-1.4 各任务已带测试）

- [ ] **Step 1: 全量运行后端测试**

Run: `cd backend && python -m pytest -q`
Expected: 全部 PASS（确认 1.1-1.5 验收点：已有口径去重 / Emby 未配置回退 / active_tasks 终态与未开播剔除均有覆盖）

- [ ] **Step 2: 若有失败，按 systematic-debugging 修复**（见 build 执行约束）

- [ ] **Step 3: 勾选 tasks.md 1.1-1.5**

编辑 `docs/openspec/changes/episode-status-and-detail-polish/tasks.md`，将 1.1-1.5 复选框改为 `[x]`。

- [ ] **Step 4: 提交**

```bash
git add docs/openspec/changes/episode-status-and-detail-polish/tasks.md
git commit -m "chore(comet): 勾选 tasks 1.1-1.5（后端 Emby 聚合 + active_tasks 完成）"
```

---

## Task 5: 前端类型扩展 + format 纯函数（勾 2.1 + 2.2 前置）

对应 tasks.md：2.1

**Files:**
- Modify: `frontend/src/types/index.ts`（`MediaDetail` 接口）
- Modify: `frontend/src/utils/format.ts`（新增 `episodeSummaryText` 纯函数）
- Test: `frontend/src/utils/format.test.ts`（追加断言）

**Interfaces:**
- Produces: `ActiveTask` 类型（`{season: number | null; episode: string; status: string; source: 'dq' | 'task'; air_date: string | null}`）、`MediaDetail.active_tasks?: ActiveTask[]`；`episodeSummaryText(stats, mediaType, seriesStatus) -> string`

- [ ] **Step 1: 写失败测试**

在 `frontend/src/utils/format.test.ts` 末尾追加：

```typescript
import { episodeSummaryText } from './format'

describe('episodeSummaryText（已有 N 缺失 M）', () => {
  it('tv + available/missing/total 已知 → 已有 N 缺失 M', () => {
    expect(episodeSummaryText({ available: 5, total: 20, missing: 15 }, 'tv', null)).toBe('已有 5 缺失 15')
  })
  it('missing 缺失 → 回退旧「已有 N / total 集」', () => {
    expect(episodeSummaryText({ available: 5, total: 20 }, 'tv', null)).toBe('已有 5 / 20 集')
  })
  it('total 未知 → 已有 N 集', () => {
    expect(episodeSummaryText({ available: 5 }, 'tv', null)).toBe('已有 5 集')
  })
  it('movie → seriesStatusLabel 回退', () => {
    expect(episodeSummaryText(null, 'movie', 'ended')).toBe('已完结')
  })
  it('无统计 → 占位符', () => {
    expect(episodeSummaryText(null, 'tv', null)).toBe('—')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: FAIL（`episodeSummaryText` 未定义）

- [ ] **Step 3: 实现类型与纯函数**

a) `frontend/src/types/index.ts` 的 `MediaDetail` 接口（第 92 行附近）增加：

```typescript
  /** episode-status-and-detail-polish：进行中任务（仅 tv；source=dq 用下载队列字典、task 用探测队列字典） */
  active_tasks?: ActiveTask[]
```

并在文件内（`EpisodeStats` 附近）新增：

```typescript
/** 详情页「当前进行中任务」行（后端 get_media.active_tasks 契约） */
export interface ActiveTask {
  season: number | null
  episode: string
  status: string
  source: 'dq' | 'task'
  air_date: string | null
}
```

b) `frontend/src/utils/format.ts` 新增（放在 `episodeStatusType` 之后）：

```typescript
/**
 * 影视列表卡集数摘要（episode-status-and-detail-polish）：
 * tv + available/missing 已知 → 「已有 N 缺失 M」；total 未知 → 「已有 N 集」；
 * movie → seriesStatusLabel 回退（电影无集数概念）。
 */
export function episodeSummaryText(
  stats: { available?: number; total?: number; missing?: number; downloaded?: number } | null | undefined,
  mediaType: string | null | undefined,
  seriesStatus: string | null | undefined,
): string {
  if (mediaType === 'movie') return seriesStatusLabel(seriesStatus, 'movie')
  if (!stats) return '—'
  const avail = stats.available ?? stats.downloaded
  const total = stats.total
  const missing = stats.missing
  if (avail !== undefined && missing !== undefined && total !== undefined) return `已有 ${avail} 缺失 ${missing}`
  if (avail !== undefined && total !== undefined) return `已有 ${avail} / ${total} 集`
  if (total !== undefined) return `共 ${total} 集`
  if (avail !== undefined) return `已有 ${avail} 集`
  return '—'
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/types/index.ts frontend/src/utils/format.ts frontend/src/utils/format.test.ts
git commit -m "feat(frontend): 新增 ActiveTask 类型与 episodeSummaryText 纯函数（已有 N 缺失 M）"
```

---

## Task 6: MediaListView episodeText 接入新文案（勾 2.2）

对应 tasks.md：2.2

**Files:**
- Modify: `frontend/src/views/MediaListView.vue`（`episodeText` 函数，第 64-74 行）

**Interfaces:**
- Consumes: `episodeSummaryText`（Task 5 产物）

- [ ] **Step 1: 修改 `episodeText`**

将 `MediaListView.vue` 中 `episodeText` 函数体替换为：

```typescript
function episodeText(m: MediaItem): string {
  return episodeSummaryText(m.episode_stats, m.media_type, m.series_status)
}
```

并在 `<script setup>` 的 import 列表（`../utils/format` 中）追加 `episodeSummaryText`。

- [ ] **Step 2: 构建与既有测试验证**

Run: `cd frontend && npx vue-tsc --noEmit && npx vitest run`
Expected: 类型检查通过、既有测试全绿（MediaListView 无独立测试文件，靠类型 + 现有测试回归）

- [ ] **Step 3: 提交**

```bash
git add frontend/src/views/MediaListView.vue
git commit -m "feat(frontend): 列表卡集数文案接入「已有 N 缺失 M」"
```

---

## Task 7: MediaDetailView 新增「当前进行中任务」区块（勾 2.3）

对应 tasks.md：2.3

**Files:**
- Modify: `frontend/src/views/MediaDetailView.vue`（script 区 + 模板「集数状态」panel 上方）
- Test: `frontend/src/views/MediaDetailView.test.ts`（追加断言）

**Interfaces:**
- Consumes: `detail.active_tasks`（Task 5 类型）、`taskQueueStatusLabel/Type/Color`、`downloadQueueStatusLabel/Type/Color`、`episodeDisplayName`（均已在 format.ts）

- [ ] **Step 1: 写失败测试**

在 `frontend/src/views/MediaDetailView.test.ts` 追加（沿用文件既有 detail reactive + mount 模式；先确认文件末尾结构再追加）：

```typescript
describe('当前进行中任务区块', () => {
  it('渲染进行中任务（集号 + 集名 + 按 source 的状态标签）', async () => {
    detail.active_tasks = [
      { season: 1, episode: 'S01E001', status: 'downloading', source: 'dq', air_date: '2026-01-01' },
      { season: 1, episode: 'S01E002', status: 'probing', source: 'task', air_date: null },
    ]
    const wrapper = mount(MediaDetailView, { global: { stubs: EP_STUBS } })
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('当前进行中任务')
    expect(text).toContain('S01E001')
    expect(text).toContain('下载中')  // dq dictionary
    expect(text).toContain('S01E002')
    expect(text).toContain('探测中')  // task dictionary
  })

  it('无任务时隐藏区块', async () => {
    detail.active_tasks = []
    const wrapper = mount(MediaDetailView, { global: { stubs: EP_STUBS } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('当前进行中任务')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts`
Expected: FAIL（区块不存在）

- [ ] **Step 3: 实现区块**

a) script 区新增：

```typescript
/** 进行中任务行（active_tasks 契约见 types.ActiveTask；source 决定状态字典） */
const activeTasks = computed(() => detail.value?.active_tasks ?? [])
```

b) import 列表追加 `taskQueueStatusColor`、`downloadQueueStatusColor`（`taskQueueStatusLabel/Type`、`downloadQueueStatusLabel/Type`、`episodeDisplayName` 已 import）。

c) 模板：在「集数状态」panel（`v-if="detail.media_type !== 'movie'"`，第 274 行）**之前**插入：

```vue
      <!-- 当前进行中任务（episode-status-and-detail-polish：仅 tv + 有任务时展示） -->
      <div v-if="detail.media_type !== 'movie' && activeTasks.length > 0" class="lc-panel">
        <h3 class="lc-panel-title">当前进行中任务</h3>
        <el-table :data="activeTasks" size="small" style="width: 100%">
          <el-table-column label="集" width="110">
            <template #default="{ row }">
              <span>{{ row.episode }}</span>
            </template>
          </el-table-column>
          <el-table-column label="名称" min-width="140">
            <template #default="{ row }">
              <span>{{ episodeDisplayName(row) || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120" align="center">
            <template #default="{ row }">
              <el-tag
                :type="row.source === 'task' ? taskQueueStatusType(row.status) : downloadQueueStatusType(row.status)"
                :color="(row.source === 'task' ? taskQueueStatusColor(row.status) : downloadQueueStatusColor(row.status)) ?? undefined"
                effect="plain"
              >
                {{ row.source === 'task' ? taskQueueStatusLabel(row.status) : downloadQueueStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts`
Expected: PASS（含新增 2 个用例）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/views/MediaDetailView.vue frontend/src/views/MediaDetailView.test.ts
git commit -m "feat(frontend): 详情页新增「当前进行中任务」区块（空态隐藏，状态标签按 source 复用字典）"
```

---

## Task 8: 详情页状态下拉宽度修复（勾 2.4）

对应 tasks.md：2.4

**Files:**
- Modify: `frontend/src/views/MediaDetailView.vue`（第 255 行 `el-select`）
- Test: `frontend/src/views/MediaDetailView.test.ts`（追加断言）

- [ ] **Step 1: 写失败测试**

在 `frontend/src/views/MediaDetailView.test.ts` 追加：

```typescript
describe('状态下拉宽度', () => {
  it('el-select 设置显式 min-width', async () => {
    const wrapper = mount(MediaDetailView, { global: { stubs: EP_STUBS } })
    await flushPromises()
    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    expect(select.attributes('style') ?? '').toContain('min-width')
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts`
Expected: FAIL（当前无 style 属性）

- [ ] **Step 3: 实现**

第 255 行：

```vue
            <el-select v-model="form.status" size="small" style="min-width: 132px">
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/views/MediaDetailView.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/views/MediaDetailView.vue frontend/src/views/MediaDetailView.test.ts
git commit -m "fix(frontend): 详情页状态下拉设置 min-width，选中文本完整可见"
```

---

## Task 9: 前端测试总验证（勾 2.5）

对应 tasks.md：2.5

**Files:** 无新增（Task 5-8 已带测试）

- [ ] **Step 1: 全量运行前端测试与构建**

Run: `cd frontend && npx vue-tsc --noEmit && npx vitest run && npm run build`
Expected: 类型检查通过、测试全绿、构建成功

- [ ] **Step 2: 若有失败，按 systematic-debugging 修复**

- [ ] **Step 3: 勾选 tasks.md 2.1-2.5**

编辑 `docs/openspec/changes/episode-status-and-detail-polish/tasks.md`，将 2.1-2.5 复选框改为 `[x]`。

- [ ] **Step 4: 提交**

```bash
git add docs/openspec/changes/episode-status-and-detail-polish/tasks.md
git commit -m "chore(comet): 勾选 tasks 2.1-2.5（前端展示与测试完成）"
```

---

## Task 10: 集成验证（勾 3.1）

对应 tasks.md：3.1

**Files:** 无代码改动（验证任务）

- [ ] **Step 1: 前后端构建与测试**

Run: `cd backend && python -m pytest -q`
Run: `cd frontend && npm run build`
Expected: 全部通过

- [ ] **Step 2: 本地启动后端，确认列表/详情接口在无真实 Emby 时回退不报错**

Run: 启动后端（`cd backend && uvicorn app.main:app` 或项目指定方式），确认：
- `GET /api/media` 返回 200，`episode_stats.available/missing` 为回退口径
- `GET /api/media/{tv_id}` 返回 200，`active_tasks` 字段存在或为空数组

> 注：若本机无法启动服务验证（服务管理规则），此项标注「待用户验证」，不阻塞 guard。

- [ ] **Step 3: 勾选 tasks.md 3.1**

编辑 `docs/openspec/changes/episode-status-and-detail-polish/tasks.md`，将 3.1 复选框改为 `[x]`（真实环境部分可标注「待用户验证」）。

- [ ] **Step 4: 提交**

```bash
git add docs/openspec/changes/episode-status-and-detail-polish/tasks.md
git commit -m "chore(comet): 勾选 tasks 3.1（集成验证完成）"
```

---

## Self-Review（已执行）

- **Spec coverage**：media-status（已有口径去重 / Emby 实际入库 / 未配置回退 / 电影回退）→ Task 2/5/6；media-detail-ui（进行中任务展示 / 未到首播日不展示 / 终态不展示 / 空态 / 下拉宽度）→ Task 3/7/8。无遗漏。
- **Placeholder scan**：所有步骤含真实代码与命令；测试给出完整断言。
- **Type consistency**：`ActiveTask`（Task 5）与后端 `active_tasks` 字段（Task 3）字段名一致（season/episode/status/source/air_date）；`episodeSummaryText`（Task 5）被 Task 6 以相同签名调用；`get_ingested_episode_codes`（Task 1）被 Task 2 以相同签名调用。
