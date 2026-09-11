# Brainstorm Summary

- Change: episode-status-and-detail-polish
- Date: 2026-09-11

## 确认的技术方案（草案，待用户最终确认）

### 1. 后端：Emby 已入库集数聚合（D1）
- emby.py 新增 `get_ingested_episode_codes(tmdb_id, title) -> set[str]`：
  - 组合既有 `find_emby_id` + `list_episodes` → 归一 code 集合（"SxxExx"）
  - 进程内 TTL 缓存（模块级 dict，TTL 6h，模式对齐 tmdb.py `_SEASON_AIR_TTL`）
  - 缓存 key 拼接 Emby 配置指纹（base_url+api_key 短哈希）→ 配置变化自然失效（tasks 1.2 要求）
  - find_emby_id 未命中（不在 Emby）→ 缓存空 set；Emby 故障（EmbyUnavailable）不缓存、上抛
- media.py `list_media`：
  - 用现有 `_rank`（rank==1）构建 done_codes 集合
  - 仅对 tv + tmdb_id + missing>0 的影视触发 Emby 聚合（asyncio.gather 并发）
  - `available = len(done_codes ∪ emby_codes)`，`missing = max(0, total - available)`
  - Emby 故障 → available 回退 done 计数，接口不报错（spec Scenario: Emby 未配置回退）

### 2. 后端：详情 active_tasks（D2）
- get_media 新增字段：
  - 数据源：download_queue（status in `_DQ_ACTIVE_STATUSES`）∪ task_queue（status in `_TQ_ACTIVE_STATUSES`）
  - 每项 `{season, episode, status, source: "dq"|"task", air_date}`
  - air_date 从 tmdb_episodes 全集轴构建 (season,episode)->air_date 查找表（覆盖所有季；tmdb_episodes 缺失 → None）
  - 过滤：code ∈ in_emby_codes → 剔除（已入库）；air_date 已知且未来 → 剔除（未开播）；air_date 未知 → 保留（宽松）
  - 排序 season,episode 升序；仅 tv 返回，movie 不返回该字段
- 复用详情接口既有 in_emby_codes（Emby 故障降级为空 set → 全部保留，宽松正确）

### 3. 前端
- types/index.ts：MediaDetail 增加 `active_tasks?: ActiveTask[]`；episodeText 改用 available+missing
- MediaListView episodeText：tv 且 total 已知 → 「已有 N 缺失 M」；total 未知 → 「已有 N 集」；movie → seriesStatusLabel 回退
- MediaDetailView：新增「当前进行中任务」区块（**紧凑 el-table + 隐藏空态**，已确认）；状态标签按 source 选 taskQueueStatusLabel / downloadQueueStatusLabel（D3 复用既有字典）
- 状态下拉：el-select 显式 min-width，选中文本完整可见

### 4. 测试策略
- 后端 pytest：缓存命中 / 配置变化失效 / Emby 故障回退；_stats 入库∪已完成去重、missing 不为负；active_tasks 终态剔除、未到首播日剔除、已入库剔除、source 与排序、空数组
- 前端 vitest：episodeText 新文案与回退；active_tasks 渲染与空态隐藏；下拉宽度

## 关键取舍与风险
- 列表页 Emby 查询：仅对「本系统显示有缺失」的 tv 影视触发 + TTL 缓存 + 并发，控制外部服务放大
- Emby 未配置/不可达：列表/详情均降级不报错（spec 已定）
- air_date 缺失：宽松保留任务展示（不误删）
- missing 取 max(0,·)：防 TMDB 全集数滞后导致负值

## 测试策略
见上「4. 测试策略」。

## Spec Patch
无（delta spec 已覆盖全部验收场景）。
