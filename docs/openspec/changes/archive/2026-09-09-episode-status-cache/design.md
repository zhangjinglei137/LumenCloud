# Design: episode-status-cache

## Context

现状（参见 proposal.md - Why）：
- 影视列表 episode_stats 与详情 episode_state 的聚合统计存在缺陷，导致「已有 x/xx 集」与「集数状态」恒为 0
- TMDB 集信息（每集标题/首播日期/季集号）目前仅有进程内 TTL 缓存（`get_tv_all_episodes` 6h / `get_tv_season_air_dates` 6h），进程重启即失效，且每次详情查询仍需回源
- 标记状态（已在库/巡检中/异常/未开播）无统一判定
- 单集大小取自 download_queue.file_size，疑似存在共享占位值（全部相同）的问题

既有资产：
- `TmdbCache` 表已存影视级元数据（title/poster/tv_status/number_of_episodes），模式可扩展
- `get_tv_all_episodes` 已实现「TV 全部正片季每集信息」回源（season/episode/air_date/name），降级语义良好
- episode_state / task_queue / download_queue 三表均有 episode 维度状态

## Goals / Non-Goals

**Goals**
- 修正列表/详情集数统计，展示真实已入库集数与单集状态
- 集信息落库持久化 + 每日定时刷新，替代进程内缓存作为持久数据源
- 统一标记状态机（已在库/巡检中/异常/未开播）
- 单集展示真实文件大小
- 入库完成后自动更新集数状态

**Non-Goals**
- 影视详情页 UI 布局重设计（media-detail-ui 负责）
- 巡检/下载队列页面重构（queue-inspection-rework 负责）
- Emby 库分类、设置页、Docker 时区

## Decisions

### D1：集信息缓存表独立于 TmdbCache 新建

**决策**：新建 `episode_info_cache` 表（media_id 或 tmdb_id 维度，存每集 season/episode/name/air_date），而非把集列表塞进 TmdbCache 的 JSON 列。

**理由**：TmdbCache 是影视级标量字段缓存，主键为 tmdb_id 字符串；集信息是列表结构且需按集查询/更新。独立表允许 `UNIQUE(tmdb_id, season, episode)`、按 media 粒度刷新、未来按集关联 episode_state。JSON 列会牺牲可查询性与增量更新能力。

**备选**：TmdbCache 加 JSON 列存储全集列表 → 简洁但无法按集查询、刷新粒度粗糙、与现有标量字段语义混杂，否决。

### D2：集信息每日刷新独立定时任务

**决策**：新增 scheduler 任务（`episode_info_refresh`），默认每日一次（system_config 键 `episode_info_refresh_interval_hours`，默认 24），遍历已入库的 tv media 调用现有 `get_tv_all_episodes` 回源并 upsert 到 `episode_info_cache`。

**理由**：复用已实现的回源逻辑（含 zh-CN 语言、season 过滤、降级语义），任务与现有 scheduler 模式一致（get_job_enabled 开关 + system_config 可调）。每日刷新兼顾「开播新集」的时效性。

**备选**：跟随全局巡检（60min）顺带刷新 → 频率过高且耦合巡检，TMDB 限流风险，否决。

### D3：标记状态机集中归一，展示层输出

**决策**：在 media 列表/详情 API 聚合处新增统一判定函数 `resolve_episode_status(ep)`，输入为单集在 episode_state/task_queue/download_queue/Emby 收录/首播日期的综合数据，输出归一状态：`in_library`（已在库）/ `scanning`（巡检中）/ `error`（异常）/ `not_aired`（未开播）/ `pending`（待定）。

**理由**：当前状态分散在多个表且口径不一（spec media-status）。集中函数保证列表与详情一致，且后续 media-detail-ui 与 queue-inspection-rework 复用同一状态源。

**判定优先级**：已在库 > 异常 > 巡检中 > 未开播 > 待定（已在库最权威，异常优先于巡检中以便用户关注）。

### D4：单集大小修正——按 download_queue / episode_state 的 file_size 独立取值

**决策**：详情集数状态逐行使用 `download_queue.file_size`（回退 episode_state.file_size），禁止任何「取第一个文件大小」的共享逻辑；列表 episode_stats 不涉及大小。前端缺失时显示「—」。

**理由**：两表 file_size 字段本为真实字节数（BigInteger）。根因是展示层/聚合层误用了共享占位。修正点在 `_dq_episode_dto`/`_task_queue_dto` 与前端展示。

### D5：入库完成后自动更新——在 library_check finalize 处联动

**决策**：`library_check._finalize_done` 完成单集入库时，同步更新该集状态（写 episode_state 或标记在库）并让列表/详情聚合自然反映；不做独立事件总线。

**理由**：library_check 已是入库闭环的唯一 finalize 点，最小改动即可让聚合数据自洽。避免引入消息队列等重机制。

## Risks / Trade-offs

- [每日刷新任务对 TMDB 限流] → 串行逐影视刷新 + 失败跳过 + system_config 可调间隔；单影视失败不影响其余
- [新增表迁移风险] → 沿用 alembic 手写迁移模式，双后端（SQLite/PG）兼容（BIG_PK/Text 约定）
- [状态机口径变更影响现有展示] → 前端 format.ts 状态映射同步扩展新状态文案与颜色，避免未知状态裸显示
- [详情接口额外查询集缓存表] → 单次 LEFT JOIN / 聚合查询，避免 N+1；无缓存时返回空列表不阻断

## Migration Plan

1. alembic 新增迁移：创建 `episode_info_cache` 表
2. 后端：models 新增 ORM、services/tmdb 增加写缓存入口、scheduler 注册 `episode_info_refresh` 任务、media 路由聚合修正（D3/D4）
3. 前端：format.ts 新增状态映射；MediaListView/MediaDetailView 展示修正
4. 部署后首轮每日任务自动填充缓存；验证详情集信息非空且列表集数非 0
5. 回滚：表与任务可安全移除，不影响既有队列流程（聚合降级为现状）

## Open Questions

- 集信息缓存是否需要按「已入库影视」还是「全部 tracking 影视」刷新？默认全部 tv media，量级小可接受，无需阻塞
- 「异常」是否需要在详情页区分「未搜索到资源」与「下载失败」子原因？默认仅归一到异常，子原因可由现有 error 字段展示，不阻塞
