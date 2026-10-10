# Brainstorm Summary

- Change: gate-missing-episodes-by-airdate
- Date: 2026-10-10

## 已确认事实（来自代码勘察）

- 缺失判定入口：`scan._scan_one` 调 `_emby_missing_codes(media)`（`backend/app/tasks/scan.py:377`），常规 tv 路径返回 code 列表，movie/未收录返回 `[None]`。
- `emby.get_missing_episodes` 已 aired-only：剔除 `PremiereDate` 已知且在未来的集；**日期缺失/无法解析 → 保守保留**（本 bug 根因）。
- `emby._normalize_episode` 已保留 `premiere_date`（原始 ISO 串或 None）。
- TMDB 已有 `get_tv_season_air_dates(tmdb_id, season) -> {ep: air_date|None}`（`tmdb.py:455`）与 `get_tv_all_episodes(tmdb_id) -> [{season,episode,air_date,name}]`（`tmdb.py:496`），6h 缓存；**失败与空结果都降级为空，无法区分**。
- TMDB 现有调用点：`routers/media.py` 详情页（`get_tv_season_air_dates`）；改动返回类型需同步这里。
- 全量兜底路径：`movie_missing=True`（tv 未收录软全量 / 真电影整部缺失）；tv 分支用 `tv_full_mode` + `_full_mode_accept(media, file_name, total_episodes)`（`scan.py:1081`）+ `_FULL_MODE_ENQUEUE_LIMIT` 限批。
- `_emby_missing_codes` 契约目前被 `test_scan_baseline.py` 等断言为 code 列表字符串；改为 dict 契约需同步这些测试。

## 候选技术方案（待用户确认）

- 门控位置：scan 缺失集构建层（D1）。
- 播出判定：TMDB `air_date` 为主；TMDB 该季成功但不含该集 → 回退 Emby `premiere_date`；两源皆无 → 视为未播出（排除）（D2）。
- 失败降级：`get_tv_season_air_dates`/`get_tv_all_episodes` 引入 `None` 失败哨兵；TMDB 失败 → 受影响集本轮 fail-safe 跳过并记录，不误判为未播出、不回退保守保留（D3）。
- 全量兜底：`get_tv_all_episodes` 建已播出集集合，逐文件按 (s,e) 门控 + 无已播出集时短路（D4）。

## 关键取舍与风险（候选）

- 无日期即未播出 → 数据源长期缺日期的已播集漏搜（用户已确认接受）。
- TMDB 进入巡检关键路径 → 故障期按 D3 失败跳过，仅延迟不误入队。
- 全量路径新增 TMDB 全季取数 → 6h 缓存 + 仅 tv 未收录触发。
- `_emby_missing_codes` 契约变化 → 同步更新 `test_scan_baseline.py` 等。

## 测试策略（候选）

- 纯函数单测：已播出判定（无日期/未来/已过/非法格式）。
- tmdb 失败哨兵单测。
- scan 门控单测：无日期不入队、未来不入队、已过且库缺正常入队、TMDB 失败 fail-safe、全量路径拦截、全部剔除短路。
- 回归：`test_scan_*`、`test_library_check*`、`test_emby_aired.py`。

## Spec Patch（候选）

- 在 `specs/media-pipeline/spec.md` 增补场景：「同剧部分集已播、部分未播 → 仅已播集入队」。

## 用户确认结论（2026-10-10）

- 用户确认采用 D1–D4 全部方案：TMDB 为主 + Emby `premiere_date` 回退；扩展 `_emby_missing_codes` 返回契约并同步更新相关测试。无待确认项。
