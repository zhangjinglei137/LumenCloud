# Design: media-detail-ui

## Context

现状（参见 proposal.md - Why）：
- MediaDetailView 当前有转存队列区块，与巡检/下载队列功能重叠
- 大小设置、巡检设置（扫描间隔）位置分散在详情表单中
- 集数状态区与 TMDB 全集列表并列展示，空间受限；全集数无分组导航
- TMDB 集名称已有数据结构（详情接口 tmdb_episodes 字段，episode-status-cache 落地后为持久缓存），当前未在集数状态行展示名称

依赖：episode-status-cache 的 media-status/episode-cache 能力提供归一化集数状态、真实大小与集信息缓存数据（tmdb_episodes）。

## Goals / Non-Goals

**Goals**
- 移除转存队列区块
- 大小 + 巡检设置并排置于 detail-header 最右侧
- 集数状态区最大显示；100 集分组 tag 导航；按范围过滤展示
- 集数行展示 TMDB 集名称

**Non-Goals**
- 集数状态数据正确性（episode-status-cache 负责）
- 队列页面（queue-inspection-rework 负责）
- 后端 API 契约变更（本 change 纯前端）

## Decisions

### D1：布局采用 flex 重构 detail-header

**决策**：detail-header 改为 flex 布局，左区承载标题/状态/操作，右区固定一组并排控件（大小 + 巡检设置），利用空白空间让二者自然靠右。

**理由**：flex 让「最右侧并排」随容器宽度自适应；避免绝对定位的脆弱性。

**备选**：绝对定位到 header 右缘 → 容器宽度变化时易溢出，否决。

### D2：转存队列区块直接移除

**决策**：删除 MediaDetailView 中的转存队列模板区块及对应 store/API 调用（如无其他页面复用）。

**理由**：该信息已由任务/下载队列承载；移除减少页面复杂度。删除前用 grep 确认无其他依赖。

### D3：集数分组 tag 纯前端计算

**决策**：分组逻辑由 computed 生成：按总集数（tmdb_episodes 或 episode_state 的最大集号）每 100 集生成 `[{label: '1-100', start, end}, ...]`；选择状态存 ref；过滤展示行按 `episode_number ∈ [start, end]` 判断。电影（media_type=movie）跳过分组。

**理由**：无需后端参与，计算量小（数百集级），响应即时。

**备选**：后端分页/分组参数 → 增加 API 复杂度，且分组是纯展示导航，否决。

### D4：集名称展示映射

**决策**：建立 `episode_number → name` 映射（来自 detail.tmdb_episodes），集数状态行渲染时取名称；无则回退 SxxExx。

**理由**：与现有 tmdbEpisodes computed 数据同源，映射 O(1) 查找。

## Risks / Trade-offs

- [分组边界判定与集号格式不一致（如 S01E001 vs S01E1）] → 以 episode_number 数值判定范围，与 format 展示解耦
- [移除转存队列误伤其他依赖] → 变更前 grep 引用点，确认仅详情页使用
- [header 右侧并排在窄屏挤压标题] → 响应式：窄屏换行，不强制一行

## Migration Plan

纯前端改动：单次 PR 完成；无迁移/回滚数据库操作。回退 = 还原 MediaDetailView 相关改动。

## Open Questions

无（设计决策均已确定，不改变 spec 或任务拆分）。
