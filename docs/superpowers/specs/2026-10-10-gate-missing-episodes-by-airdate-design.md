---
comet_change: gate-missing-episodes-by-airdate
role: technical-design
canonical_spec: openspec
---

# 缺集巡检「已播出」门控 — 技术设计

> 上游：`docs/openspec/changes/gate-missing-episodes-by-airdate/{proposal,design}.md` 与 `specs/media-pipeline/spec.md`（canonical spec）。
> 本文件只做深度技术细化，不改需求范围。

## 1. 背景与现状（实现层）

- `backend/app/tasks/scan.py:1696 _scan_one` 是巡检主流程：状态预检 → `_emby_missing_codes`（Emby 防重基线）→ `_resolve_done_states` → `_search_and_rank`（cloudSaver 搜索）→ 遍历分享/文件匹配 → `_enqueue`。
- `scan._emby_missing_codes`（`scan.py:377`）现返回 `list[str | None] | None`：
  - tv + Emby 命中 → 缺失集 code 列表（`["S03E01", ...]`）；
  - tv 未收录 + `scan_baseline_required=False`（默认）→ `[None]`（全量模式）；
  - movie → 已在库 `[]` / 整部缺失 `[None]`；
  - `None` 仅表示强防重下基线不可用（本轮跳过）。
- `emby.get_missing_episodes`（`emby.py:300`）已 aired-only：`PremiereDate` 已知且在未来 → 剔除；**日期缺失/无法解析 → 保守保留**。`_normalize_episode` 已保留 `premiere_date`。
- `tmdb.get_tv_season_air_dates`（`tmdb.py:455`）→ `{episode_number: air_date|None}`；`tmdb.get_tv_all_episodes`（`tmdb.py:496`）→ `[{season,episode,air_date,name}]`；二者 6h 进程内缓存，**失败与空结果都降级为空**，无法区分。
- 这两者当前仅被 `routers/media.py` 详情页使用；本次首次进入巡检关键路径。
- 全量兜底：`_scan_one` 中 `movie_missing`（`missing` 含 `None`）+ `tv_full_mode`（`movie_missing and media_type!="movie"`）；tv 全量走 `_full_mode_accept`（`scan.py:1081`）逐文件校验 + `_FULL_MODE_ENQUEUE_LIMIT` 限批。

## 2. 目标 / 非目标

**目标**：巡检缺失集判定统一「已播出」门控（常规 + 全量兜底）；TMDB 为主、Emby 兜底；两源皆无 → 未播出；TMDB 失败 fail-safe。

**非目标**：不改 `library_check`/`recovery` 口径、不改前端展示口径、不改 Emby 服务 aired-only 契约形状、不改搜索匹配/排序算法本身。

## 3. 方案总览

### 3.1 常规路径

```
_scan_one
  ├─ missing = _emby_missing_codes(media)          # 契约改为 list[dict|None]|None
  │      dict = {"code": "S03E01", "premiere_date": str|None}
  ├─ [NEW] missing = await _filter_aired(media, missing)   # 已播出门控
  │      仅保留已播出集；被剔除者不进入后续关键词/搜索/入队
  ├─ missing_keys = {m["code"] for m in missing if m}
  ├─ movie_missing = any(m is None for m in missing)
  └─ ... 既有流程不变（_resolve_done_states → _search_and_rank → 入队）
```

### 3.2 全量兜底路径（tv 未收录）

```
movie_missing 且 media_type != "movie"
  ├─ [NEW] aired_map = await tmdb.get_tv_all_episodes(media.tmdb_id)
  │      None → 本轮 fail-safe 跳过（不入队、记录）
  │      非空但无任何已播出集 → 短路跳过（未开播剧，不搜索）
  │      否则 → {(season, episode): aired_bool}
  └─ 遍历文件时：
         解析 (s,e) → known_unaired → 拒绝；aired → 交给既有 _full_mode_accept
```

## 4. 详细设计

### 4.1 `tmdb` 失败哨兵（区分「失败」与「无日期」）

- `get_tv_season_air_dates(...) -> dict[int, str | None] | None`：回源失败 / 非 200 / JSON 异常 / api_key 缺失 → `None`；成功（含成功但空季）→ `dict`（可为空）。缓存策略不变（仅成功写缓存）。
- `get_tv_all_episodes(...) -> list[dict] | None`：整体失败（tv 详情非 200 / 网络异常 / api_key 缺失）→ `None`；成功 → `list`。单季失败仍跳过该季（保持现有语义），但**至少有一个正片季回源失败**时是否整体判 `None` 需明确：本设计选「任一被纳入的正片季取数失败即视为未完成」→ 返回 `None`（避免把失败季误当「无该集」而漏判）。实现上记录失败标志，聚合末尾据此返回 `None`。
- 适配调用点：`routers/media.py` 详情页把 `None` 按「无数据」处理（`air_map` 退化为空，行为与今日空结果一致），不影响详情接口。

### 4.2 已播出判定纯函数

放 `backend/app/utils.py`（与 `fmt_episode` / `parse_episode_num` 同层，便于单测）：

```
def episode_aired(air_date: str | None, today: date) -> bool:
    # air_date 为 "YYYY-MM-DD"；None/空/非法 → False（未播出）
    # 已播出 ⇔ 解析成功且 air_date <= today
```

比较用**本地日期**（`date.today()`），与 `routers/media.py resolve_episode_status` 现有口径一致。

### 4.3 常规门控 `_filter_aired(media, missing)`

输入 `missing: list[dict|None]`（来自 `_emby_missing_codes`），输出同型。规则：

1. `None` 项（全量/电影哨兵）原样保留。
2. movie：不适用（跳过门控）。
3. tv：收集缺失集涉及季 `seasons = {season_of(code)}`；对每季调 `tmdb.get_tv_season_air_dates(tmdb_id, season)`：
   - 任一季返回 `None`（失败）→ **该季标记 fail-safe**：该季的缺失集本轮跳过（不判缺失、不搜索、不入队），并记录降级原因。其余季正常判定。
   - 季成功 → 用该季 `{ep: air_date}` 判定该集：
     - `TMDB 有 air_date` → `episode_aired(air_date, today)`；已播出保留，未播出剔除。
     - `TMDB 无该集条目 或 air_date=None` → 回退 Emby：若该项 `premiere_date` 可解析且已过 → 保留；否则剔除（未播出）。
4. `media.tmdb_id` 缺失：无法回源 TMDB。此时仅有 Emby `premiere_date` 可用 —— 按「Emby 有日期且已过 → 保留，否则剔除（未播出）」判定，并记录「无 tmdb_id 降级为 Emby 单源」。
5. 全部集被剔除后，`missing_keys` 为空且非全量 → 命中既有「无遗漏短路」（`scan.py:1772`），不空跑搜索。

> 注：Emby 基线已在 `get_missing_episodes` 剔除「未来 PremiereDate」，故到此处 `premiere_date` 已是「已过或缺失」；门控仍需处理「缺失」。

### 4.4 `_emby_missing_codes` 契约扩展

返回 `list[dict | None] | None`，每项 `{"code": str, "premiere_date": str | None}`；`None` 哨兵语义不变。tv 路径：

```
missing = await emby.get_missing_episodes(emby_id)
return [{"code": ep.get("code"), "premiere_date": ep.get("premiere_date")}
        for ep in missing if ep.get("code")]
```

`_scan_one` 内 `missing_keys` 改由 `m["code"]` 派生；其余消费点（`movie_missing = any(m is None ...)`）不变。

### 4.5 全量兜底门控

- 在 `_scan_one` 全量分支（`tv_full_mode`）进入文件循环前：
  - `aired_map = await tmdb.get_tv_all_episodes(media.tmdb_id)`；
  - `None` → 本轮 fail-safe 跳过（等价「无遗漏短路」路径，但 message 区分「TMDB 取数失败跳过」）；
  - 成功但 `aired_set` 为空（无任何已播出集）→ 短路跳过（未开播剧）；
  - 否则构建 `aired_by_key = {(s,e): aired_bool}`。
- `_full_mode_accept` 增加参数或在调用处追加门控：解析文件 `(s,e)`（复用既有 `_RE_SXXEXX` / `parse_episode_num`）：
  - 命中 `aired_by_key` 且 `aired=False` → 拒绝（`unrelated_filtered`/新计数）；
  - 命中且 `aired=True` → 继续既有 `_full_mode_accept`；
  - 无法解析 `(s,e)`（无季号/纯数字）→ 由既有 `_full_mode_accept` 决定（保持不误杀）。
- `media.tmdb_id` 缺失 → 无法建 map → 按 4.1/4.3 的降级：跳过全量（不盲搜）。

### 4.6 短路与计数一致性

- 门控剔除只影响「是否缺失」，不改 `scan_detail` 结构；`missing_total` 用门控后的基线。
- fail-safe 跳过走 `_finish("error"/"skipped", ...)` 并在 message 标明「TMDB 播出日期取数失败，本轮跳过」。

## 5. 边界条件

| 情形 | 处理 |
|---|---|
| 剑来 S03E01：TMDB/Emby 均无播出日期 | 视为未播出 → 不搜索下载 |
| TMDB 有日期且在将来 | 未播出 → 剔除 |
| TMDB 无该集、Emby 有日期且已过 | 已播出 → 保留 |
| TMDB 该季成功、该集 air_date=None、Emby 也无 | 未播出 → 剔除 |
| TMDB 回源失败（`None`） | 该季/该剧本轮 fail-safe 跳过，记录；不误判 |
| 无 `tmdb_id` | tv 常规：仅 Emby 单源判定；全量：跳过 |
| 全部门控剔除 | 命中「无遗漏短路」，不触发搜索 |
| 电影整部缺失 | 不套用 tv 门控（保持原行为） |

## 6. 测试策略

- 单测（纯函数）：`episode_aired` 覆盖 None/空/非法/未来/今天/过去。
- 单测（tmdb）：`get_tv_season_air_dates`/`get_tv_all_episodes` 的「失败→None」「成功空→dict/list」。
- 单测（scan 常规门控）：无日期不入队（剑来场景）、未来不入队、已过且库缺正常入队、TMDB 失败 fail-safe 跳过、全部门控剔除短路不搜索、部分集已播仅已播入队。
- 单测（scan 全量）：`get_tv_all_episodes` 失败跳过、无已播出集短路、known_unaired 文件被拒。
- 回归：`test_scan_baseline.py`（契约改 dict）、`test_scan_full_mode_filter.py`、`test_scan_run_phases.py`、`test_scan_numeric_match.py`、`test_scan_silent_filter.py`、`test_library_check*.py`、`test_emby_aired.py`。
- 集成：构造已过/未来/无日期三组，执行一次巡检，仅已过且库中缺失的集入队。

## 7. 风险 / 取舍

- [无日期即未播出 → 数据源长期缺日期的已播集漏搜] → 用户已确认接受（宁漏搜不误搜）；数据补齐后自动恢复。
- [`get_tv_all_episodes` 单季失败按整体失败处理 → 可能更保守地跳过] → 权衡：宁可延迟，不误判；6h 缓存降低触发频率。
- [`_emby_missing_codes` 契约变化 → 测试与潜在调用点回归] → 调用点收敛（仅 `_scan_one` + 测试），逐点更新。
- [TMDB 进入关键路径，故障放大] → D3 失败仅延迟本轮，不误入队、不误停。

## 8. Spec Patch

在 `specs/media-pipeline/spec.md` 的 MODIFIED 需求下增补场景：

> #### Scenario: 同剧部分已播、部分未播仅已播集入队
> - **WHEN** 同一剧既有已播出（日期 ≤ 今天）又有未播出（日期在未来或无日期）的缺失集
> - **THEN** 仅已播出集被判定为缺失并进入搜索入队，未播出集被排除，二者互不影响
