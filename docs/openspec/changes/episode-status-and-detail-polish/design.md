## Context

详情页/列表页的集数与任务状态目前已具备数据底座：`list_media` 内 `_stats` 从 download_queue ∪ task_queue ∪ episode_state 三表 IN 查询聚合并按状态优先级合并去重，total 取 `tmdb_cache.number_of_episodes`；`episode_info_cache` 提供了含首播日期的全集元数据；详情页已有「全部 / 1-100 / 101-200」分组导航与 TMDB 集名称展示（见 specs/media-detail-ui）。Emby 侧已有 `find_emby_id`（按 ProviderId / searchTerm）但无「按影视统计已入库集数」的聚合查询。见 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 列表「已有 N 缺失 M」的统计口径对齐用户实际可观看状态：Emby 入库 ∪ 本系统下载完成（去重）
- 详情页进行中任务区展示「已到首播日、尚未入库、当前正在处理」的集，与全集分组互补
- 状态下拉宽度修复

**Non-Goals:**
- 不引入任务催收/重试等操作能力，本 change 只做展示
- 不改 Emby 影视库分类/条目查询（由 emby-library-all-poster change 覆盖）
- 不改变下载队列任务流转

## Decisions

**D1: 「已有」统计以 Emby 实际入库为准，本系统已完成集数兜底合并**
- 理由：用户反馈「S01E157 应在 Emby 中却显示没有」，说明本系统队列状态滞后于 Emby 实际收录；Emby 是最终可观看载体
- 实现方式：后端新增 Emby 聚合查询（按 ParentId/ProviderId 关联影视 → 统计该影视已入库集号集合），与 episode_state 已完成集号做并集
- 备选：仅用本系统表（实现快但口径失真）→ 放弃

**D2: 进行中任务区块数据由后端详情接口直接提供，而非前端启发式推导**
- 理由：任务状态只存在于 download_queue/task_queue，前端推导需重复状态机逻辑且易漂移
- 后端在详情接口输出 `active_tasks: [{season, episode, status, air_date}]`（过滤：进行中状态 ∩ 已到首播日 ∩ 未入库）

**D3: 状态字段与文案复用既有 DOWNLOAD_QUEUE_STATUS_MAP，不新建状态定义**
- 理由：避免展示层与队列层状态字典分叉

## Risks / Trade-offs

- [Emby 查询放大/延迟] → 复用进程内 TTL 缓存 + 仅对 tv/series 且有缺失的影视触发 Emby 聚合
- [Emby 未配置/不可达导致列表失败] → 回退到本系统已完成集数，接口不报错
- [「已到首播日」依赖首播日数据完整] → episode_info_cache 缺失时按集号与当前时间宽松处理（不误删任务展示）

## Migration Plan

- 后端详情接口新增字段为增量（optional），前端按字段存在性渲染，无破坏性变更
- 无数据库迁移

## Open Questions

无。