# Brainstorm Summary

- Change: episode-status-cache
- Date: 2026-09-09

## 确认的技术方案

### 关键决策（用户已确认）
1. **total 总集数**：TMDB 全集数优先（tmdb_cache.number_of_episodes），无 TMDB 数据时回退现有三表聚合
2. **已在库判定**：本地 done（download_queue/episode_state status=done）**或** Emby 收录（in_emby）均视为「已在库」
3. **详情集数状态展示范围**：TMDB 全集 + 本地状态合并视图（全集可见，状态列反映各自判定），为 media-detail-ui 的 100 集分组导航提供数据

### 技术方案（D1-D5 深化）
- **D1 集信息缓存表**：新建 `episode_info_cache`（tmdb_id+season+episode UNIQUE，存 name/air_date），独立于 TmdbCache 影视级标量；alembic 迁移，双后端兼容
- **D2 每日刷新任务**：scheduler 注册 `episode_info_refresh`（默认 24h，system_config `episode_info_refresh_interval_hours`），复用 `get_tv_all_episodes` 回源，串行遍历 tv media，单影视失败跳过
- **D3 标记状态机**：`resolve_episode_status(ep)` 统一归一（已在库 > 异常 > 巡检中 > 未开播 > 待定），输入综合 download_queue/task_queue/episode_state/Emby 收录/首播日期
- **D4 单集大小**：逐行取 download_queue.file_size（回退 episode_state.file_size），禁止共享占位；详情集数状态逐集真实值
- **D5 入库联动**：library_check._finalize_done 完成时同步更新该集在库状态，聚合自然反映

### 详情接口输出结构（用户确认 3 后）
- episode_state 输出改为「TMDB 全集轴 + 本地状态合并」：全集每集都有行，状态/大小/更新时间列按本地记录填充，无记录显示待定/未开播
- tmdb_episodes 字段与 episode_state 合并视图共用集信息缓存

## 关键取舍与风险

- [TMDB 限流] → 每日一次串行 + 失败跳过 + system_config 可调
- [迁移风险] → alembic 手写迁移，BIG_PK/Text 双后端约定
- [状态口径变更影响前端] → format.ts 同步扩展映射
- [详情接口 N+1] → 集缓存表单次 LEFT JOIN/聚合查询
- [total 用 TMDB 全集数，无数据回退聚合] → 已有 number_of_episodes 字段，回退语义与现状一致

## 测试策略

- 后端 pytest：集信息缓存读写/失败保留、每日刷新任务、resolve_episode_status 优先级、列表统计（5/20 场景）、详情合并视图、入库联动、大小真实值
- 前端 vitest：状态文案映射、episodeText 统计、集数状态行渲染

## Spec Patch

无新增验收场景；现有 delta spec（episode-cache / media-status）已覆盖上述决策，不需回写修改。
