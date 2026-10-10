## Context

巡检缺集判定的现状（详见 proposal.md - Why）：

- `backend/app/tasks/scan.py` 以 Emby 遗漏集（`emby.get_missing_episodes`）作为「缺失」基线，再构造关键词 → cloudSaver 搜索 → 匹配 → 入队。
- `emby.get_missing_episodes` 已做 aired-only：剔除 `PremiereDate` **已知且在未来**的集；但对**日期缺失/无法解析**的集**保守保留** → 未开播且无日期的集（剑来 S03E01）被当缺失反复搜索下载。
- `backend/app/services/tmdb.py` 已有 `get_tv_season_air_dates`（单季每集首播日期）与 `get_tv_all_episodes`（全季每集首播日期），6h 进程内缓存，目前仅详情页展示使用，**未参与巡检门控**；二者失败均降级为空，无法区分「失败」与「确实无日期」。
- 前端/详情页 `media.py` 已用 TMDB `air_date` 判 `not_aired`，但「无日期」按保留处理，与下载口径不一致（本次不改展示层）。

## Goals / Non-Goals

**Goals:**
- 巡检缺失集判定统一应用「已播出」门控：已播出（日期已知且 ≤ 今天）且库中不存在 → 缺失；否则（含无日期）→ 非缺失、不搜索、不入队。
- 覆盖常规巡检（Emby 遗漏集基线）与全量兜底路径（Emby 未收录该剧）。
- 播出日期以 TMDB 为准，复用既有 TMDB 取数能力与缓存。
- 明确数据源失败时的降级语义，避免故障期误判/停搜。

**Non-Goals:**
- 不改 `library_check` 入库确认与 `recovery` 回退口径（本轮外）。
- 不改前端详情页缺失计数 / 状态 tag 口径。
- 不解决「已播出后搜索结果误匹配无关网剧」的排序/匹配问题（门控只减少过早搜索，不改变匹配算法）。
- 不改 Emby 侧数据与 `get_missing_episodes` 对外契约形状（保持返回含 `premiere_date` 的基线）。

## Decisions

### D1. 门控位置：在 scan 缺失集构建层统一实施
将「已播出」判定上移到 `scan.py` 的缺失集构建层（常规路径紧邻 `_emby_missing_codes` 主线、全量路径在 `movie_missing` 分支），而非改 Emby 服务内部过滤。
- 理由：播出日期权威源是 TMDB，Emby 只提供基线；保持 `emby.get_missing_episodes` 返回包含 `premiere_date` 的原始基线，门控由调用方统一。
- 备选：改 Emby 服务——否决，会把 TMDB 依赖引入 Emby 服务层，且 Emby 的 `PremiereDate` 常缺未开播集日期，单靠它无法判定。

### D2. 播出日期判定规则（TMDB 优先，Emby 兜底）
对每个候选集 (season S, episode E)：
1. 查 TMDB：命中且 `air_date ≤ 今天` → 已播出；命中且 `air_date > 今天` → 未播出（排除）。
2. TMDB 无该集信息（该季成功返回但无此集）：回退 Emby `premiere_date`——已知且 `≤ 今天` → 已播出；未来或缺失 → 未播出（排除）。
3. 两源均无日期 → **视为未播出（排除）**（本次核心行为变更）。
- 比较粒度：以**日期（本地日期）**比较 `air_date`（`YYYY-MM-DD`），`今天 >= air_date` 即已播出；与 `media.py` 现有 `date.fromisoformat` 口径一致。
- 备选：Emby 优先——否决，Emby 缺日期是本次问题根因。

### D3. 数据源失败降级：可区分「失败」与「无日期」，失败走 fail-safe 跳过
现有 `get_tv_season_air_dates` / `get_tv_all_episodes` 失败与空结果都返回空，若直接套用「无日期=未播出」会把 TMDB 故障误判为「全区未播出」。
- 加固：为二者引入失败哨兵（失败返回 `None`，成功但无集返回 `[]`/`{}`），不改正常返回值语义。
- `scan`：TMDB 取数失败 → 受影响集**本轮 fail-safe 跳过**（不入队、不判缺失），记录原因，下一轮恢复后按正常门控；**不**把失败当「无日期」永久排除，**也**不回退到「无日期保守保留」（避免复发原 bug）。
- 备选：失败回退 Emby 保守保留——否决，故障期会重现剑来式误搜。

### D4. 全量兜底路径同样门控
Emby 未收录该剧（`movie_missing` 且 media_type≠movie）时 `missing_keys` 为空、无目标季，无法按缺失集反推季。改为：用 `get_tv_all_episodes(tmdb_id)` 构建「已播出集集合」，全量搜索得到的候选文件在既有 `_full_mode_accept` 校验中追加「该集已播出」门控；无 tmdb_id 或取数失败按 D3 跳过。
- 理由：与常规路径同一口径，避免未开播剧被整部误搜。

### D5. 缓存与调用量
复用 TMDB 6h 进程内缓存；常规路径按缺失集涉及季调用 `get_tv_season_air_dates`（每季一次），全量路径调用 `get_tv_all_episodes`（其内部已含逐季取数缓存）。

## Risks / Trade-offs

- [无日期即未播出 → 已播出但 TMDB 长期缺日期的集永不下载] → 该类集在 TMDB 补齐日期后自动恢复；符合用户明确选择（宁漏搜不误搜）。后续如需可加宽限期，属独立演进。
- [TMDB 进入巡检关键路径，其故障影响面扩大] → D3 fail-safe 跳过而非误判；仅延迟本轮，不产生错误入队。
- [全量路径新增 TMDB 全季取数，调用量上升] → 6h 缓存 + 仅当 `movie_missing` 且 tv 时触发。
- [TMDB 与 Emby 日期冲突] → D2 以 TMDB 为准；Emby 仅作 TMDB 无该集时的兜底，降低冲突面。
- [改动触及 `get_missing_episodes` 语义边界] → 保持 Emby 服务契约不变（仍返回含 `premiere_date` 的 aired-only 基线），门控只加在 scan 层，降低回归面；`test_library_check.py` 等现有测试需回归验证。
