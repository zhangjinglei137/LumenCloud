# Comet Design Handoff

- Change: media-detail-ui
- Phase: design
- Mode: compact
- Context hash: f255caf384c55dc33a29f413d2cd9de52364a24507f12a6381972bf12740116f

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/media-detail-ui/proposal.md

- Source: docs/openspec/changes/media-detail-ui/proposal.md
- Lines: 1-26
- SHA256: 97f03141bf7b9d687d97c8fedaf8e55d598e90d46f5fd4bd361e0ceab0d2ca20

```md
## Why

影视详情页当前布局与功能不符合使用习惯：转存队列区块冗余；大小与巡检设置位置分散；集数状态展示空间不足且全集数查找困难（无分组导航）；无法直观看到每集的 TMDB 名称。需要重设计详情页，让信息层级清晰、集数浏览高效。

## What Changes

- **移除转存队列区块**：影视详情不再展示转存队列（该能力已由下载/任务队列承接）
- **布局调整**：大小与巡检设置移到 lc-panel detail-header 最右侧并排展示
- **集数状态最大显示**：集数状态区获得最大显示空间
- **集数分组导航**：集数按每 100 集一个 tag 分组（如 350 集 → 1-100 / 101-200 / 201-300 / 301-350），点击 tag 过滤展示对应范围
- **TMDB 集名称展示**：集数行展示 TMDB 集信息名称（依赖 episode-status-cache 的集信息缓存数据）
- 纯前端 UI 变更，不涉及后端 API 契约改动

## Capabilities

### New Capabilities
- `media-detail-ui`: 影视详情页的信息层级、布局与集数分组导航展示能力

### Modified Capabilities
<!-- 纯前端布局与交互调整，无 spec 级行为契约变更（集数状态数据来自 episode-status-cache 的 media-status capability），故无既有 capability 的需求级变更。 -->

## Impact

- 前端：`frontend/src/views/MediaDetailView.vue`（布局/集数分组/转存队列移除）、`frontend/src/stores/media.ts`（如需字段）、`frontend/src/utils/format.ts`（分组辅助）
- 依赖：episode-status-cache 提供集信息缓存数据（tmdb_episodes 名称）；无后端 API 改动
- 无数据库、无配置变更

```

## docs/openspec/changes/media-detail-ui/design.md

- Source: docs/openspec/changes/media-detail-ui/design.md
- Lines: 1-68
- SHA256: 7b482008765fef152fff6382cecf9c6de6f478839f03e273234ba6cf05aa2cff

```md
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

```

## docs/openspec/changes/media-detail-ui/tasks.md

- Source: docs/openspec/changes/media-detail-ui/tasks.md
- Lines: 1-21
- SHA256: c6e1b3e0b9dcf1bedea0523baaa35cbbf87180a4bb594d8fa973d3a0c683e038

```md
# Tasks: media-detail-ui

## 1. 布局重构

- [ ] 1.1 MediaDetailView detail-header 改为 flex 布局，左区标题/状态/操作、右区并排「大小 + 巡检设置」控件组，验证窄屏不溢出且右侧并排展示
- [ ] 1.2 移除影视详情转存队列模板区块与对应 store/API 调用（先 grep 确认无其他页面依赖），验证页面不再显示转存队列且构建通过

## 2. 集数状态区与分组导航

- [ ] 2.1 集数状态区调整为最大显示空间（占主要内容区域），验证集数状态区主导布局且单集列表完整可见
- [ ] 2.2 新增 computed 分组逻辑：按总集数每 100 集生成分组 tag（350 集 → 1-100/101-200/201-300/301-350；电影不分组），验证分组 tag 正确生成
- [ ] 2.3 分组选择状态 ref + 过滤展示（按 episode_number ∈ [start,end]），验证选择分组后集数状态区只显示对应范围行

## 3. TMDB 集名称展示

- [ ] 3.1 建立 episode_number → name 映射（来自 detail.tmdb_episodes），集数状态行展示名称（无则回退 SxxExx），验证有名称显示名称、无名称回退集号不报错

## 4. 验证

- [ ] 4.1 前端测试补充：分组生成（350 集边界、电影不分组）、名称回退用例，验证 `vitest` 通过
- [ ] 4.2 手动验证：350 集剧集详情分组导航切换、集数名称展示、header 右侧并排、转存队列消失，验证 `npm run build` 通过且页面交互符合预期

```

## docs/openspec/changes/media-detail-ui/specs/media-detail-ui/spec.md

- Source: docs/openspec/changes/media-detail-ui/specs/media-detail-ui/spec.md
- Lines: 1-57
- SHA256: deb1c22689441b94e0e8e4c1520b342cbb9cc0e0d0fc2618962ee0782395a889

```md
## Purpose

为影视详情页提供清晰的信息层级与高效的集数浏览：移除转存队列区块，大小与巡检设置并排置于 detail-header 最右侧，集数状态获得最大显示空间，集数按每 100 集分组导航并可查看 TMDB 集名称。

## ADDED Requirements

### Requirement: 移除转存队列区块

影视详情页 SHALL 不再展示转存队列区块；转存相关信息由巡检/下载任务队列页面承载。

#### Scenario: 详情页无转存队列
- **WHEN** 用户打开影视详情
- **THEN** 页面不再显示转存队列区块

### Requirement: 大小与巡检设置并排于 header 最右侧

影视详情页的「大小」与「巡检设置」控件 SHALL 并排展示在 lc-panel detail-header 的最右侧。

#### Scenario: header 布局
- **WHEN** 用户打开影视详情
- **THEN** 大小与巡检设置并排位于 detail-header 最右侧，不与其他控件混杂

### Requirement: 集数状态最大显示

影视详情页集数状态区域 SHALL 获得最大显示空间（横向/纵向优先），以完整展示单集状态列表。

#### Scenario: 集数状态占主导
- **WHEN** 用户查看影视详情
- **THEN** 集数状态区占用主要显示区域，信息完整可见

### Requirement: 集数按 100 集分组导航

剧集集数 SHALL 按每 100 集一组生成分组 tag（如 350 集 → 1-100 / 101-200 / 201-300 / 301-350）；用户选择某分组 tag 后，集数状态区 SHALL 只展示该范围内的集数。电影不分组。

#### Scenario: 生成分组 tag
- **WHEN** 某剧集共 350 集
- **THEN** 详情页显示 1-100 / 101-200 / 201-300 / 301-350 四个分组 tag

#### Scenario: 按分组过滤展示
- **WHEN** 用户选择 101-200 分组 tag
- **THEN** 集数状态区仅展示 101-200 集的单集行

#### Scenario: 电影不分组
- **WHEN** 影视类型为电影
- **THEN** 不显示分组 tag，集数状态区直接展示

### Requirement: 展示 TMDB 集名称

集数状态行 SHALL 展示对应集的 TMDB 名称（来自集信息缓存）；无名称时回退为集号（SxxExx），不展示空字段。

#### Scenario: 展示集名称
- **WHEN** 某集存在 TMDB 集信息且含名称
- **THEN** 该集状态行展示名称与集号

#### Scenario: 无名称回退
- **WHEN** 某集无 TMDB 名称或集信息缓存为空
- **THEN** 该集状态行仅展示集号，不报错

```
