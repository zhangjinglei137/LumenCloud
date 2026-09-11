# Comet Design Handoff

- Change: episode-status-and-detail-polish
- Phase: design
- Mode: compact
- Context hash: 8839e3c32efeb2886add00c603ebd6f19c639d55604999a15d3d9f643a105234

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/episode-status-and-detail-polish/proposal.md

- Source: docs/openspec/changes/episode-status-and-detail-polish/proposal.md
- Lines: 1-26
- SHA256: 6825390e98c3bd58a1b26d9afba61dc7fb771c7d1635989395722994ac80424a

```md
# Proposal: episode-status-and-detail-polish

## Why

影视列表卡的「已有 0/xx 集」聚合逻辑缺失 Emby 实际入库维度，导致实际已在 Emby 的集数显示为 0，与真实状态严重不符；影视详情页缺少「当前进行中任务」区块，用户无法直观看到哪些集正在搜索/等待/下载；详情页「状态」下拉宽度过窄，选中后看不到选项文本。

## What Changes

- **列表卡集数统计改为「已有 N 缺失 M」**：已有 = Emby 实际入库集数 ∪ 已下载完成集数（去重），缺失 = TMDB 全集 - 已有；修复聚合逻辑导致的恒 0 问题
- **详情页新增「当前进行中任务」区块**：保留 TMDB 全集分组（全部 / 1-100 / 101-200 …）与集数状态区；新增进行中任务展示，覆盖搜索中、等待中、下载中（含转存中/刮削中/入库确认等全部进行中状态），完成/失败/跳过状态不展示；仅展示已到首播日且尚未入库的集（如 S01E157 已到首播日但不在 Emby → 展示；S01E158 未到首播日 → 不展示）
- **修复详情页「状态」下拉宽度**：显式设置下拉宽度，保证选中后选项文本完整可见

## Capabilities

### New Capabilities
<!-- 无新增 capability，全部归入既有能力修改 -->

### Modified Capabilities
- `media-status`: 影视列表集数统计真实展示（新增 Emby 实际入库聚合维度、「已有 N 缺失 M」展示语义）
- `media-detail-ui`: 影视详情集数状态展示（新增「当前进行中任务」区块与状态宽度）

## Impact

- 后端：`backend/app/routers/media.py`（`_stats` 聚合增加 Emby 入库维度、详情接口任务区块数据）、`backend/app/services/emby.py`（新增查询影视已入库集数能力）
- 前端：`frontend/src/views/MediaListView.vue`（集数文案）、`frontend/src/views/MediaDetailView.vue`（进行中任务区块、下拉宽度）、`frontend/src/utils/format.ts`（展示辅助）、`frontend/src/types/index.ts`（类型扩展）
- 依赖：Emby 服务端（按影视/库查询已入库集）；无数据库变更

```

## docs/openspec/changes/episode-status-and-detail-polish/design.md

- Source: docs/openspec/changes/episode-status-and-detail-polish/design.md
- Lines: 1-43
- SHA256: 5567de243728aba43e3ca33f2e205511af2ae4eab3fe44ac9ab983ffef9f8041

```md
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
```

## docs/openspec/changes/episode-status-and-detail-polish/tasks.md

- Source: docs/openspec/changes/episode-status-and-detail-polish/tasks.md
- Lines: 1-20
- SHA256: 875f1f563fc3b5e3349899b102bb826b8626ef814aedbb8df6f79e136c24acc5

```md
# Tasks: episode-status-and-detail-polish

## 1. 后端：Emby 已入库集数聚合

- [ ] 1.1 services/emby.py 新增查询影视已入库集数能力（按影视关联库/ProviderId 统计已入库 SxxExx 集合），复用 _get/_check_config/错误归一，验证未配置/不可达抛 EmbyUnavailable
- [ ] 1.2 services/emby.py 为已入库集数查询加进程内 TTL 缓存，验证重复查询命中缓存且配置变化后失效
- [ ] 1.3 routers/media.py 的 _stats 聚合扩展「已有」口径：Emby 已入库集号 ∪ episode_state/download_queue/task_queue 已完成集号（去重），缺失 = TMDB 全集 − 已有，验证现有三表聚合逻辑仍工作且 Emby 维度计入
- [ ] 1.4 routers/media.py 详情接口新增 active_tasks 字段：从 download_queue/task_queue 取进行中状态集（过滤完成/失败/跳过）并附带 season/episode/status/air_date，验证只返回进行中任务
- [ ] 1.5 补充后端测试：已有口径（入库∪已完成去重）、Emby 未配置回退、active_tasks 过滤（终态剔除、未到首播日剔除），验证 `pytest` 通过

## 2. 前端：列表与详情展示

- [ ] 2.1 frontend/src/types/index.ts 扩展 MediaItem/MediaDetail 类型（已有/缺失统计、active_tasks 字段），验证类型契约与后端一致
- [ ] 2.2 MediaListView.vue episodeText 改为「已有 N 缺失 M」文案（可用 available/total 或新增字段换算），验证列表卡展示正确、电影仍回退状态文案
- [ ] 2.3 MediaDetailView.vue 新增「当前进行中任务」区块：基于 detail.active_tasks 渲染集号 + 状态标签（复用 DOWNLOAD_QUEUE_STATUS_MAP），验证仅展示进行中任务、空态正常
- [ ] 2.4 MediaDetailView.vue 状态下拉 el-select 显式设置宽度（min-width），验证选中后文本完整可见
- [ ] 2.5 前端测试补充：episodeText 新文案、active_tasks 区块渲染与空态、下拉宽度，验证 `vitest` 通过

## 3. 集成验证

- [ ] 3.1 `npm run build` 与后端启动无报错，列表/详情页在本地环境正常展示（后端无真实 Emby 时回退不报错）
```

## docs/openspec/changes/episode-status-and-detail-polish/specs/media-detail-ui/spec.md

- Source: docs/openspec/changes/episode-status-and-detail-polish/specs/media-detail-ui/spec.md
- Lines: 1-30
- SHA256: ae8f085968761ad5c16c0d6877d80d62df1e53581004b8fa067a1a09f171a8fc

```md
# media-detail-ui Delta Spec

## ADDED Requirements

### Requirement: 详情页展示当前进行中任务

影视详情页 SHALL 在集数状态区展示「当前进行中任务」：仅展示状态为进行中（搜索中 / 等待中 / 下载中 / 转存中 / 刮削中 / 入库确认等非终态）且已到首播日的集任务；已到首播日但不在 Emby 的集作为缺失任务展示；未到首播日的集不展示；终态（已完成 / 失败 / 跳过）一律不展示。

#### Scenario: 已到首播日且未入库的任务展示
- **WHEN** S01E157 首播日 2026-09-06（早于当前日期）且该集不在 Emby 中、当前正在搜索或下载
- **THEN** 详情页展示该集任务行（集号 + 任务状态标签：搜索中 / 等待中 / 下载中 等）

#### Scenario: 未到首播日不展示
- **WHEN** S01E158 首播日 2026-09-13（晚于当前日期）
- **THEN** 详情页不展示该集的进行中任务

#### Scenario: 终态任务不展示
- **WHEN** 某集任务已完成、失败或已跳过
- **THEN** 该集不出现在进行中任务区

#### Scenario: 空态
- **WHEN** 当前没有任何进行中任务
- **THEN** 进行中任务区显示空态文案（或隐藏区块），不报错

### Requirement: 状态下拉完整可见

影视详情页「状态」下拉选择器 SHALL 具有足够宽度，选中后所选项文本完整可见，不被截断为仅显示箭头。

#### Scenario: 状态下拉完整显示选项
- **WHEN** 用户在详情页打开「状态」下拉并选中「订阅中」或「已暂停」
- **THEN** 下拉框及选中项完整显示文本，无截断
```

## docs/openspec/changes/episode-status-and-detail-polish/specs/media-status/spec.md

- Source: docs/openspec/changes/episode-status-and-detail-polish/specs/media-status/spec.md
- Lines: 1-22
- SHA256: 3db8b2322fe10da960c9a14b2e9e78c9054668919aeb9a22bd034e880e0d3630

```md
# media-status Delta Spec

## MODIFIED Requirements

### Requirement: 影视列表集数统计真实展示

系统 SHALL 在影视列表返回每部影视的集数统计（已有集数与缺失集数），其中「已有」按实际可观看维度聚合：Emby 实际入库集数 ∪ 本系统已下载完成集数（去重）；「缺失」= TMDB 全集数 − 已有。「已有 x/xx 集」文案改为「已有 N 缺失 M」。不得因聚合逻辑缺失而恒为 0。

#### Scenario: 列表展示真实已入库集数
- **WHEN** 某剧集已有 5 集完成入库、全集 20 集
- **THEN** 列表显示「已有 5 缺失 15」，而非「已有 0/20 集」

#### Scenario: 已有包含 Emby 实际入库
- **WHEN** 某剧 1-3 集已在 Emby 库中（本系统下载/任务队列无记录）
- **THEN** 「已有」仍计入这 3 集（以 Emby 实际入库为准），缺失为 17

#### Scenario: Emby 未配置回退
- **WHEN** Emby 未配置或该影视不在任何 Emby 库中
- **THEN** 「已有」回退为已下载完成集数，缺失为全集数减去该值，不报错

#### Scenario: 电影集数统计
- **WHEN** 影视类型为电影
- **THEN** 列表不显示集数统计，回退到电影状态展示
```
