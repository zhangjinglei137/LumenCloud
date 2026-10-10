## 1. TMDB 取数加固（区分「失败」与「无日期」）

- [x] 1.1 为 `tmdb.get_tv_season_air_dates` 引入失败哨兵：回源失败返回 `None`，成功但该季无集返回 `{}`；返回类型改为 `dict | None`。适配调用点 `routers/media.py` 详情页（None 按「无数据」降级，不报错）。验证：新增/更新 `backend/tests` 单测覆盖「失败→None」「成功空季→{}」「成功有集→dict」三分支，全绿。
- [x] 1.2 为 `tmdb.get_tv_all_episodes` 引入失败哨兵：失败返回 `None`，成功返回 `list`（含被跳过的失败季需可区分或整体失败上抛，与 1.1 语义一致）；适配既有调用点。验证：单测覆盖「失败→None」「成功→非空/空 list」。
- [x] 1.3 新增「已播出」日期判定纯函数 `episode_aired(air_date, today) -> bool`（如 `app.utils`）：无日期/非法/未来→False（未播出，核心行为变更），已过→True；Emby `premiere_date` 兜底由 scan `_filter_aired` 处理。验证：单测覆盖全部分支。

## 2. 常规巡检（Emby 遗漏集基线）门控

- [x] 2.1 在 `scan.py` 缺失集构建层应用门控：对 `missing_keys` 涉及季调用 `get_tv_season_air_dates`，按 1.3 判定剔除未播出/无日期集；未播出集不进入 `missing_keys`、不参与 `_build_keywords` 与入队。验证：新增 scan 单测——「无日期集（剑来场景）不入 missing_keys、不入队」「未来日期集不入队」「已过日期且库中缺失集正常入队」。
- [x] 2.2 实现 2.1 的 TMDB 失败 fail-safe：取数返回 `None` 时，受影响集本轮跳过（不入队、不判缺失）并记录降级原因，绝不把失败当「无日期」或回退保守保留。验证：单测 mock TMDB 失败 → 本轮不新增入队且写入降级记录，下一轮成功恢复后按门控判定。
- [x] 2.3 保证门控与既有短路语义一致：全部集被门控剔除后，巡检按「无遗漏」短路结束（沿用 `missing_keys` 空 + 非全量的既有分支），不空跑搜索。验证：单测——全部门控剔除时 `scan` 返回 skipped 且不触发 cloudSaver 搜索。

## 3. 全量兜底路径门控（Emby 未收录该剧）

- [x] 3.1 在 `movie_missing` 且 `media_type != "movie"` 分支用 `get_tv_all_episodes(tmdb_id)` 构建「已播出集集合」；在 `_full_mode_accept` 的每文件校验中追加「该集已播出」门控（无集号/无法匹配仍按既有策略）。验证：单测——未开播剧（无日期/未来日期）在全量路径不产生入队任务；已开播缺失集仍可入队。
- [x] 3.2 全量路径 TMDB 失败按 2.2 同一 fail-safe 处理（返回 `None` → 本轮跳过并记录，不整部误搜）。验证：单测 mock `get_tv_all_episodes` 失败 → 全量路径本轮无新增入队且记录降级。

## 4. 回归与集成验证

- [ ] 4.1 运行后端相关测试并全绿：`pytest backend/tests/test_library_check.py`、scan 相关测试及本次新增测试（确认 `get_missing_episodes` / `_episode_in_missing` 既有契约无回归）。
- [ ] 4.2 集成验证：构造同一剧三组集（已过日期、未来日期、无日期），执行一次巡检，确认仅「已过日期且媒体库中不存在」的集进入搜索下载入队，其余两类不产生任务；记录实际输出作为证据。
