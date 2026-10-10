---
change: gate-missing-episodes-by-airdate
design-doc: docs/superpowers/specs/2026-10-10-gate-missing-episodes-by-airdate-design.md
base-ref: 03bd8580699e71bfb4929d2a6bb4133d61269729
---

# 缺集巡检「已播出」门控 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 巡检缺失集判定统一应用「已播出」门控——播出日期以 TMDB 为主、Emby 兜底，日期缺失或未播出视为未播出，不搜索下载；覆盖常规与全量兜底路径。

**Architecture:** 在 `scan._scan_one` 缺失集构建层统一实施门控；`tmdb` 两个取数函数引入 `None` 失败哨兵以区分「失败」与「无日期」；`scan._emby_missing_codes` 契约扩展为携带 `premiere_date` 的 dict 列表，以支撑 Emby 兜底。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy(async) / pytest（asyncio.run 风格）；TMDB REST / Emby REST。

**Spec:** `docs/superpowers/specs/2026-10-10-gate-missing-episodes-by-airdate-design.md`

## Global Constraints

- 产物语言：`zh-CN`（注释与文档）。
- 判定口径：已播出 ⇔ 播出日期可解析且 `播出日期 <= 本地今天`；`None`/空/非法 → 未播出。
- TMDB 为主源，Emby `premiere_date` 仅兜底；两源皆无 → 未播出（排除）。
- TMDB 取数失败（`None` 哨兵）→ fail-safe：受影响集本轮跳过，**不**回退保守保留、**不**当未播出永久排除。
- 不改 `library_check` / `recovery` 口径；不改 Emby `get_missing_episodes` 对外契约形状；不改前端。
- 提交信息用中文，Conventional Commits 结构。

---

### Task 1: 已播出判定纯函数 `episode_aired`

- [x] Task 1: 已播出判定纯函数 `episode_aired`

**Files:**
- Modify: `backend/app/utils.py`（新增函数，`fmt_episode` 附近）
- Test: `backend/tests/test_episode_aired.py`（新增）

**Interfaces:**
- Produces: `episode_aired(air_date: str | None, today: date) -> bool` —— 供 Task 4/6 使用。

- [x] **Step 1: 写失败测试**

```python
# backend/tests/test_episode_aired.py
from datetime import date
from app.utils import episode_aired

TODAY = date(2026, 10, 10)

def test_none_and_empty_not_aired():
    assert episode_aired(None, TODAY) is False
    assert episode_aired("", TODAY) is False

def test_past_date_aired():
    assert episode_aired("2026-10-09", TODAY) is True

def test_today_aired():
    assert episode_aired("2026-10-10", TODAY) is True

def test_future_not_aired():
    assert episode_aired("2026-10-11", TODAY) is False

def test_invalid_not_aired():
    assert episode_aired("not-a-date", TODAY) is False

def test_emby_datetime_string_supported():
    # Emby PremiereDate 为 ISO 8601 日期时间串
    assert episode_aired("2026-10-09T12:00:00.0000000Z", TODAY) is True
    assert episode_aired("2026-10-11T12:00:00.0000000Z", TODAY) is False
```

- [x] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_episode_aired.py -q`
Expected: FAIL（`ImportError: cannot import name 'episode_aired'`）

- [x] **Step 3: 最小实现**

```python
# backend/app/utils.py
from datetime import date
# ...
def episode_aired(air_date: str | None, today: date) -> bool:
    """该集是否已播出：播出日期可解析且不晚于 today。

    air_date 支持 "YYYY-MM-DD"（TMDB air_date）与 ISO 8601 日期时间串
    （Emby PremiereDate）；None/空/非法 → False（按未播出处理）。
    """
    if not air_date:
        return False
    try:
        return date.fromisoformat(str(air_date)[:10]) <= today
    except (ValueError, TypeError):
        return False
```

（确认 `backend/app/utils.py` 顶部是否已 `from datetime import ...`；无则补充。）

- [x] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_episode_aired.py -q`
Expected: PASS（6 passed）

- [x] **Step 5: 提交**

```bash
git add backend/app/utils.py backend/tests/test_episode_aired.py
git commit -m "feat(scan): 新增 episode_aired 已播出判定纯函数"
```

---

### Task 2: `tmdb.get_tv_season_air_dates` 引入失败哨兵

- [x] Task 2: `tmdb.get_tv_season_air_dates` 引入失败哨兵

**Files:**
- Modify: `backend/app/services/tmdb.py:455-482`
- Modify: `backend/app/routers/media.py`（详情页调用点适配 `None`，约 650-662）
- Test: `backend/tests/test_tmdb_air_sentinel.py`（新增）

**Interfaces:**
- Produces: `get_tv_season_air_dates(tmdb_id, season) -> dict[int, str | None] | None` —— 失败返回 `None`；成功返回 dict（可空）。供 Task 4 使用。

- [x] **Step 1: 写失败测试**

```python
# backend/tests/test_tmdb_air_sentinel.py
from unittest.mock import AsyncMock
import pytest
from app.services import tmdb

def test_season_air_dates_returns_none_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(tmdb, "_fetch_season_episodes", AsyncMock(return_value=None))
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(1, 1)) is None

def test_season_air_dates_returns_empty_dict_on_empty_season(monkeypatch):
    monkeypatch.setattr(tmdb, "_fetch_season_episodes", AsyncMock(return_value=[]))
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(2, 1)) == {}

def test_season_air_dates_returns_map_on_success(monkeypatch):
    monkeypatch.setattr(
        tmdb, "_fetch_season_episodes",
        AsyncMock(return_value=[{"episode_number": 1, "air_date": "2026-01-01"}]),
    )
    tmdb._SEASON_AIR_CACHE.clear()
    import asyncio
    assert asyncio.run(tmdb.get_tv_season_air_dates(3, 1)) == {1: "2026-01-01"}
```

- [x] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_tmdb_air_sentinel.py -q`
Expected: FAIL（首条断言失败：目前失败返回 `{}` 而非 `None`）

- [x] **Step 3: 最小实现**

在 `get_tv_season_air_dates` 中把失败分支从 `return {}` 改为 `return None`（缓存命中与成功写入逻辑不变）；更新 docstring 说明返回类型。示例：

```python
    episodes = await _fetch_season_episodes(tmdb_id, season_number)
    if episodes is None:
        return None  # 回源失败 → 失败哨兵（调用方据此 fail-safe）
```

- [x] **Step 4: 适配 `routers/media.py` 调用点**

详情页对 `None` 视为「无数据」（等价旧空结果），例如：

```python
            try:
                dates = await tmdb.get_tv_season_air_dates(media.tmdb_id, season)
            except Exception:
                dates = None
            if dates:
                # 合并进 air_map；None/{} 均不写入（air_date 保持 None）
```

确认详情页既有分支在 `dates` 为 `None` 时不会抛 `TypeError`（不要对 `None` 调 `.items()`/`.get()`）。若有其它调用点，一并检查（`grep -rn "get_tv_season_air_dates" backend/app`）。

- [x] **Step 5: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_tmdb_air_sentinel.py -q && python -m pytest tests/test_tmdb_cache.py -q`
Expected: PASS

- [x] **Step 6: 提交**

```bash
git add backend/app/services/tmdb.py backend/app/routers/media.py backend/tests/test_tmdb_air_sentinel.py
git commit -m "feat(tmdb): get_tv_season_air_dates 失败返回 None 哨兵"
```

---

### Task 3: `tmdb.get_tv_all_episodes` 引入失败哨兵

- [x] Task 3: `tmdb.get_tv_all_episodes` 引入失败哨兵

**Files:**
- Modify: `backend/app/services/tmdb.py:496-567`
- Modify: `backend/app/routers/media.py`（`get_tv_all_episodes` 调用点适配 `None`）
- Test: `backend/tests/test_tmdb_air_sentinel.py`（追加）

**Interfaces:**
- Produces: `get_tv_all_episodes(tmdb_id) -> list[dict] | None` —— 整体失败（api_key 缺失 / tv 详情回源失败 / 任一正片季回源失败）→ `None`；成功 → list。供 Task 6 使用。

- [x] **Step 1: 写失败测试**

```python
def test_all_episodes_returns_none_on_tv_detail_failure(monkeypatch):
    import asyncio
    from app.services import tmdb as t
    monkeypatch.setattr(t, "_now", t._now)  # 保持
    t._ALL_EPS_CACHE.clear()
    monkeypatch.setattr(t.config_store, "get", lambda k, d=None: "KEY")
    class _Resp:
        status_code = 500
    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()
    monkeypatch.setattr(t.httpx, "AsyncClient", lambda **k: _Client())
    assert asyncio.run(t.get_tv_all_episodes(99)) is None
```

（若 mock 风格过重，可改为 monkeypatch `_fetch_season_episodes` 返回 `None` 并 monkeypatch tv 详情响应；以实际可运行为准。）

- [x] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_tmdb_air_sentinel.py::test_all_episodes_returns_none_on_tv_detail_failure -q`
Expected: FAIL（目前返回 `[]`）

- [x] **Step 3: 最小实现**

- api_key 缺失 → `return None`；
- tv 详情非 200 / 异常 → `return None`；
- 遍历正片季时记录 `season_failed`：任一季 `_fetch_season_episodes` 返回 `None` → 置 `season_failed=True`；
- 聚合末尾：`if season_failed: return None`（不写缓存）；否则 `return out`（写缓存策略不变）。
- 更新 docstring 返回类型。

- [x] **Step 4: 适配调用点**

`grep -rn "get_tv_all_episodes" backend/app`，详情页对 `None` 按空列表降级（`or []`）。

- [x] **Step 5: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_tmdb_air_sentinel.py -q`
Expected: PASS

- [x] **Step 6: 提交**

```bash
git add backend/app/services/tmdb.py backend/app/routers/media.py backend/tests/test_tmdb_air_sentinel.py
git commit -m "feat(tmdb): get_tv_all_episodes 失败返回 None 哨兵"
```

---

### Task 4: `_emby_missing_codes` 契约扩展 + 常规门控 `_filter_aired`

- [ ] Task 4: `_emby_missing_codes` 契约扩展 + 常规门控 `_filter_aired`

**Files:**
- Modify: `backend/app/tasks/scan.py:377-419`（`_emby_missing_codes`）
- Modify: `backend/app/tasks/scan.py`（新增 `_filter_aired`；`_scan_one` 接入）
- Test: `backend/tests/test_scan_airdate_gate.py`（新增）

**Interfaces:**
- Consumes: `episode_aired`（Task 1）、`tmdb.get_tv_season_air_dates`（Task 2）。
- Produces:
  - `_emby_missing_codes(media) -> list[dict | None] | None`，dict = `{"code": str, "premiere_date": str | None}`。
  - `_filter_aired(media, missing: list[dict | None]) -> list[dict]`，并记录本季失败（下条 Task 处理 fail-safe 短路）。

- [ ] **Step 1: 写失败测试（契约）**

```python
# backend/tests/test_scan_airdate_gate.py
import asyncio, types
from unittest.mock import AsyncMock
from datetime import date
from app.tasks import scan as scan_mod

def run(c): return asyncio.run(c)

def _tv(emby_id="e1", tmdb_id=42):
    return types.SimpleNamespace(tmdb_id=tmdb_id, title="剑来", media_type="tv", id=1)

def test_emby_missing_codes_returns_dicts(monkeypatch):
    monkeypatch.setattr(scan_mod.emby, "find_emby_id", AsyncMock(return_value="e1"))
    monkeypatch.setattr(scan_mod.emby, "get_missing_episodes", AsyncMock(return_value=[
        {"code": "S03E01", "premiere_date": None},
    ]))
    out = run(scan_mod._emby_missing_codes(_tv()))
    assert out == [{"code": "S03E01", "premiere_date": None}]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py::test_emby_missing_codes_returns_dicts -q`
Expected: FAIL（当前返回 `["S03E01"]`）

- [ ] **Step 3: 实现契约扩展**

`_emby_missing_codes` 末段改为：

```python
    missing = await emby.get_missing_episodes(emby_id)
    return [
        {"code": ep.get("code"), "premiere_date": ep.get("premiere_date")}
        for ep in missing if ep.get("code")
    ]
```

movie 与未收录分支保持不变（`[]` / `[None]` / `None`）。

- [ ] **Step 4: 写门控失败测试**

```python
def test_filter_aired_drops_no_airdate(monkeypatch):
    # TMDB 该季成功但不含该集 → 回退 Emby（也无）→ 未播出 → 剔除
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={}))
    missing = [{"code": "S03E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [] and failed == set()

def test_filter_aired_keeps_aired(monkeypatch):
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={1: "2020-01-01"}))
    missing = [{"code": "S01E01", "premiere_date": None}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept == [{"code": "S01E01", "premiere_date": None}]

def test_filter_aired_embys_fallback(monkeypatch):
    # TMDB 无该集，Emby 有已过日期 → 保留
    monkeypatch.setattr(scan_mod.tmdb, "get_tv_season_air_dates",
                        AsyncMock(return_value={}))
    missing = [{"code": "S01E01", "premiere_date": "2020-01-01T00:00:00Z"}]
    kept, failed = run(scan_mod._filter_aired(_tv(), missing))
    assert kept and kept[0]["code"] == "S01E01"
```

- [ ] **Step 5: 实现 `_filter_aired`**

```python
async def _filter_aired(media, missing):
    """已播出门控：返回 (kept 缺失项, 本季取数失败的季集合)。

    - None 哨兵项原样保留（全量/电影）；
    - movie 不套用门控；
    - tv：按季取 TMDB air_date，逐集 episode_aired；TMDB 无该集时回退 Emby premiere_date；
      两源皆无 → 未播出剔除；
    - 某季 TMDB 取数失败 → 记入 failed（该季集不保留），由调用方决定 fail-safe。
    """
    from datetime import date as _date
    kept, failed = [], set()
    seasons = {}
    for m in missing:
        if m is None or not m.get("code"):
            kept.append(m)  # None 哨兵原样保留
            continue
        if (media.media_type or "").lower() == "movie":
            kept.append(m)
            continue
        s = _season_of_key(m["code"])
        seasons.setdefault(s, []).append(m)
    if (media.media_type or "").lower() == "movie":
        return kept, failed
    today = _date.today()
    for s, items in seasons.items():
        air_map = None
        if media.tmdb_id is not None:
            air_map = await tmdb.get_tv_season_air_dates(media.tmdb_id, s)
        if media.tmdb_id is not None and air_map is None:
            failed.add(s)
            logger.warning("[scan] media=%s 季 %s TMDB air_date 取数失败，本轮跳过该季", media.id, s)
            continue
        for m in items:
            ep = _parse_episode_number(m["code"])
            air = (air_map or {}).get(ep) if (air_map is not None and ep is not None) else None
            if air is not None:
                if episode_aired(air, today):
                    kept.append(m)
            elif episode_aired(m.get("premiere_date"), today):
                kept.append(m)  # TMDB 无该集 → Emby 兜底
            # 否则剔除（未播出）
    return kept, failed
```

注：`_season_of_key` 已存在（`scan.py:720`）；季号解析用既有 `_parse_episode_number`/`_ep_num`（实现时统一用一个）。`episode_aired` 从 `app.utils` import。

- [ ] **Step 6: 运行门控测试**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_airdate_gate.py
git commit -m "feat(scan): _emby_missing_codes 携带 premiere_date 并新增 _filter_aired 门控"
```

---

### Task 5: `_scan_one` 接入常规门控 + TMDB 失败 fail-safe

- [ ] Task 5: `_scan_one` 接入常规门控 + TMDB 失败 fail-safe

**Files:**
- Modify: `backend/app/tasks/scan.py:1704-1778`（`_scan_one` 主流程）
- Test: `backend/tests/test_scan_airdate_gate.py`（追加，端到端）

**Interfaces:**
- Consumes: `_filter_aired`（Task 4）。
- 行为：门控后 `missing_keys` 由 `m["code"]` 派生；`failed` 季非空且门控后无保留且非全量 → 本轮 `skipped` 且 message 标明 TMDB 取数失败。

- [ ] **Step 1: 写端到端失败测试**

复用 `test_scan_baseline.py` 的 `_patch_scan_env` 风格（新建最小 helper），断言：
- 无日期集（`premiere_date=None` + TMDB 空）→ `_scan_one` 不触发 cloudSaver 搜索、不入队（`task_queue` 空），task_run `skipped`。
- 已播出集 → 正常入队。

```python
def test_scan_one_skips_unaired_no_date(db, monkeypatch):
    # EMby 基线返回 S03E01（无 premiere_date）；TMDB 该季空 → 门控剔除 → 短路
    # 断言 cloudsaver.search 未被调用、task_queue 无行
    ...
```

（test 需 monkeypatch：`scan_mod.async_session=db`、`scan_mod._emby_missing_codes`（或 emby.find_emby_id/get_missing_episodes）、`scan_mod.tmdb.get_tv_season_air_dates`、`cloudsaver.search` 记录调用。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py -q`
Expected: FAIL（当前会继续搜索）

- [ ] **Step 3: 接入 `_scan_one`**

在 `_emby_missing_codes` 调用与 `_phase_done(phases,"check")` 之后：

```python
    missing, aired_failed = await _filter_aired(media, missing)
    if aired_failed and not missing and not any(m is None for m in missing):
        _phase_skip_remaining(phases, "search")
        return await _finish(
            "skipped",
            f"TMDB 播出日期取数失败（季 {sorted(aired_failed)}），本轮跳过",
            touch_last_scan_at=False,
        )
```

并将后续 `missing_keys = {m for m in missing if m is not None}` 改为
`missing_keys = {m["code"] for m in missing if m and m.get("code")}`；
`movie_missing = any(m is None for m in missing)` 不变；
`scan_missing = list(missing)` 等处凡按元素取值处，按 `m["code"]`/`m is None` 适配（见 `scan.py:1757-1767`）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py -q`
Expected: PASS

- [ ] **Step 5: 回归基线测试（契约变更需同步）**

Run: `cd backend && python -m pytest tests/test_scan_baseline.py tests/test_scan_run_phases.py tests/test_scan_silent_filter.py tests/test_scan_numeric_match.py -q`
Expected: 既有断言中把 `["S01E01"]` 等改判为 dict；逐条更新后 PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/
git commit -m "feat(scan): _scan_one 接入已播出门控与 TMDB 失败 fail-safe"
```

---

### Task 6: 全量兜底路径门控

- [ ] Task 6: 全量兜底路径门控

**Files:**
- Modify: `backend/app/tasks/scan.py`（`_scan_one` 全量分支 + 文件循环 `tv_full_mode` 段，约 1734/1884）
- Test: `backend/tests/test_scan_airdate_gate.py`（追加）

**Interfaces:**
- Consumes: `tmdb.get_tv_all_episodes`（Task 3）、`episode_aired`（Task 1）。

- [ ] **Step 1: 写失败测试**

- `tv_full_mode` 且 `get_tv_all_episodes` 返回全未播出 → `_scan_one` 不入队（短路）。
- 返回含已播出集 → 对应文件入队，known_unaired 文件被拒。
- 返回 `None` → 本轮跳过、不搜索。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py -q`
Expected: FAIL

- [ ] **Step 3: 实现全量门控**

在 `_scan_one` 计算 `movie_missing`/`tv_full_mode` 之后、搜索前：

```python
    aired_by_key: dict[tuple[int, int], bool] = {}
    if movie_missing and (media.media_type or "").lower() != "movie":
        if media.tmdb_id is None:
            _phase_skip_remaining(phases, "search")
            return await _finish("skipped", "全量兜底：缺 tmdb_id，无法判定播出日期，本轮跳过",
                                 touch_last_scan_at=False)
        all_eps = await tmdb.get_tv_all_episodes(media.tmdb_id)
        if all_eps is None:
            _phase_skip_remaining(phases, "search")
            return await _finish("skipped", "全量兜底：TMDB 播出日期取数失败，本轮跳过",
                                 touch_last_scan_at=False)
        today = date.today()
        aired_by_key = {(e["season"], e["episode"]): episode_aired(e.get("air_date"), today)
                        for e in all_eps}
        if aired_by_key and not any(aired_by_key.values()):
            _phase_skip_remaining(phases, "search")
            return await _finish("skipped", "全量兜底：TMDB 无已播出集（未开播），本轮跳过",
                                 touch_last_scan_at=False)
```

文件循环 `if tv_full_mode:` 内、`_full_mode_accept` 之后追加：

```python
                    m_sxx = _RE_SXXEXX.search(file_name)
                    if m_sxx:
                        key = (int(m_sxx.group(1)), int(m_sxx.group(2)))
                        if key in aired_by_key and not aired_by_key[key]:
                            unrelated_filtered += 1
                            continue  # 未播出集文件，拒绝
```

（`date` 已在 scan.py import；确认。）

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && python -m pytest tests/test_scan_airdate_gate.py tests/test_scan_full_mode_filter.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/tasks/scan.py backend/tests/test_scan_airdate_gate.py
git commit -m "feat(scan): 全量兜底路径按 TMDB 播出日期门控"
```

---

### Task 7: 回归与集成验证

- [ ] Task 7: 回归与集成验证

**Files:**
- Test: 复用/新增

- [ ] **Step 1: 全量后端相关测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿（含 `test_library_check*.py`、`test_emby_aired.py`、`test_scan_*.py`）。

- [ ] **Step 2: 集成验证场景**

构造同剧三组集（已过日期 / 未来日期 / 无日期），执行一次 `_scan_one`，断言仅「已过日期且媒体库不存在」的集进入 `task_queue`；记录实际输出（`task_run.message` 与 `task_queue` 行）作为 verify 证据。

- [ ] **Step 3: 提交（如有测试补充）**

```bash
git add backend/tests/
git commit -m "test(scan): 补已播出门控集成验证"
```

---

## Self-Review

- **Spec coverage**：无日期/未来不搜索、已播出正常搜、部分已播仅已播入队、全量路径拦截、回源失败不整体停搜 —— 分别由 Task 4/5（常规）、Task 6（全量）、Task 5（fail-safe）覆盖。
- **Placeholder scan**：无 TBD/TODO；每个代码步骤含具体代码。
- **Type consistency**：`episode_aired(air_date, today)->bool`；`get_tv_season_air_dates->dict|None`；`get_tv_all_episodes->list|None`；`_emby_missing_codes->list[dict|None]|None`；`_filter_aired->(list[dict], set[int])` 全程一致。
