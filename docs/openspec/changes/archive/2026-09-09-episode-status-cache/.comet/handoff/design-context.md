# Comet Design Handoff

- Change: episode-status-cache
- Phase: design
- Mode: compact
- Context hash: dfe4c2892152bf1ebd0f5f6093c5b243541a7b9f750d370ede8252d82e5051a2

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/episode-status-cache/proposal.md

- Source: docs/openspec/changes/episode-status-cache/proposal.md
- Lines: 1-27
- SHA256: 5f0b2436ab04187b1ecf053f24c7741526e29635aa6fee57663f1c6c1a3636ea

```md
## Why

影视列表的「已有 0/xx 集」与影视详情的「集数状态(0)」全部显示为 0，无法反映真实已入库集数；集数信息依赖实时 TMDB 查询（慢且易失败）；标记状态（已在库/巡检中/异常/未开播）与实际不符；集大小显示错误（默认等同第一个文件大小）。需要一套正确的集数状态数据源、数据库缓存与定时刷新机制。

## What Changes

- **修复集数统计全为 0**：修正影视列表 episode_stats 与影视详情 episode_state 的聚合统计，使「已有 x/xx 集」与「集数状态」显示真实已入库集数
- **新增集数信息数据库缓存**：将 TMDB 集信息（每集标题、首播日期、季/集号）入库缓存，新增每日定时刷新任务，避免每次查询都打 TMDB
- **修正标记状态机**：统一「已在库 / 巡检中 / 异常（开播但未下载成功，含未搜索到资源）/ 未开播」的判定逻辑，影视下载入库完成后自动更新集数状态信息
- **修正集大小展示**：集大小读取每个文件的真实大小，不再默认等同第一个文件大小
- 涉及数据模型变更：新增集信息缓存表（**BREAKING** schema 变更，alembic 迁移）

## Capabilities

### New Capabilities
- `episode-cache`: TMDB 集信息数据库缓存与每日定时刷新能力，为集数状态展示提供持久化数据源
- `media-status`: 集数状态统计（已有 x/xx 集、集数状态）的正确聚合；标记状态机（已在库/巡检中/异常/未开播）；集大小真实值展示；影视下载入库完成后自动更新集数状态

### Modified Capabilities
<!-- 集数状态统计与队列调度职责分离：media-pipeline 聚焦巡检/任务/下载队列，集数统计、标记状态机、大小与入库更新归属新增的 media-status，故无既有 capability 的需求级变更。 -->

## Impact

- 后端：`app/routers/media.py`（列表/详情集数聚合）、`app/models/`（新缓存表）、`app/tasks/`（每日集信息刷新任务）、`app/services/tmdb.py`（集信息获取）、alembic 迁移
- 前端：`frontend/src/views/MediaListView.vue`（episodeText）、`frontend/src/views/MediaDetailView.vue`（集数状态行展示）、`frontend/src/utils/format.ts`（状态映射）
- 配置：system_config 新增集信息缓存刷新间隔（可配置）
- 依赖：PostgreSQL（新表）；TMDB API（每日刷新）

```

## docs/openspec/changes/episode-status-cache/design.md

- Source: docs/openspec/changes/episode-status-cache/design.md
- Lines: 1-86
- SHA256: eaa7b6207f1111da566f3085800d9998103eb1416626a6cf0b2a9ef165a752af

[TRUNCATED]

```md
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

```

Full source: docs/openspec/changes/episode-status-cache/design.md

## docs/openspec/changes/episode-status-cache/tasks.md

- Source: docs/openspec/changes/episode-status-cache/tasks.md
- Lines: 1-36
- SHA256: b9a7d9d8888d130e542c44d64219184715a80ffa45b962f5c664bcbb4eb1d6d2

```md
# Tasks: episode-status-cache

## 1. 集信息缓存表与 ORM

- [ ] 1.1 新增 `episode_info_cache` 表 alembic 迁移（字段：tmdb_id、season、episode、name、air_date，UNIQUE(tmdb_id, season, episode)，双后端兼容 BIG_PK/Text 约定），验证 `alembic upgrade head` 在 SQLite 与 PG 均成功建表
- [ ] 1.2 models/__init__.py 新增 `EpisodeInfoCache` ORM 类（映射迁移表，含索引），验证导入不报错且表元数据可查询

## 2. 集信息回源与缓存写入

- [ ] 2.1 services/tmdb.py 新增 `refresh_episode_info(tmdb_id)`：复用 `get_tv_all_episodes` 回源并将每集 upsert 到 `episode_info_cache`，验证单影视刷新后缓存表出现对应季/集/名称/首播日期行
- [ ] 2.2 services/tmdb.py 新增 `get_episode_info(tmdb_id)` 读缓存函数（无缓存返回 []），验证返回结构与 spec（season/episode/name/air_date）一致
- [ ] 2.3 新增后端测试（tests/test_tmdb.py 或新增测试文件）覆盖：刷新写缓存、刷新失败保留旧数据、读取空缓存返回 []，验证 `pytest` 通过

## 3. 每日定时刷新任务

- [ ] 3.1 scheduler.py 注册 `episode_info_refresh` 任务（默认每日一次，system_config 键 `episode_info_refresh_interval_hours` 默认 24，get_job_enabled 开关可停用），验证任务注册进 scheduler 且开关读取生效
- [ ] 3.2 实现刷新遍历：查询全部 tv media 逐个调用 refresh_episode_info，单影视失败跳过并 log warning，验证全量刷新不中断且失败不影响其余

## 4. 标记状态机与集数统计修正

- [ ] 4.1 routers/media.py 新增统一状态判定 `resolve_episode_status(ep)`（已在库>异常>巡检中>未开播>待定，输入综合 episode_state/task_queue/download_queue/Emby 收录/首播日期），验证单测覆盖各状态优先级
- [ ] 4.2 修正列表 episode_stats 聚合：可用集数按真实已入库状态统计，验证「已有 x/xx 集」不再恒为 0（构造 5/20 入库场景断言 5）
- [ ] 4.3 修正详情 episode_state 输出：逐集状态经 resolve_episode_status 归一、单集大小取 download_queue.file_size（回退 episode_state.file_size，禁止共享第一个文件大小），验证多集大小各异时逐行真实值
- [ ] 4.4 详情接口输出集信息缓存列表（调用 get_episode_info，movie/无 tmdb_id 返回 [] 不阻断），验证详情 JSON 含 tmdb_episodes 且无缓存时为空列表

## 5. 入库完成联动更新

- [ ] 5.1 library_check._finalize_done 完成单集入库时同步更新该集在库状态（写 episode_state 或标记在库，供列表/详情聚合自然反映），验证 finalize 后列表集数统计 +1
- [ ] 5.2 补充测试：入库完成触发状态更新与统计联动，验证 `pytest` 通过

## 6. 前端展示修正

- [ ] 6.1 frontend/src/utils/format.ts 新增归一状态文案与颜色映射（已在库/巡检中/异常/未开播/待定），验证 MEDIA_STATUS_MAP/集数状态映射含新状态
- [ ] 6.2 MediaListView episodeText 展示真实集数统计（有统计显示 x/xx，无统计回退），验证列表不再全为 0
- [ ] 6.3 MediaDetailView 集数状态行使用归一状态与真实大小（大小缺失显示 —），验证详情集数状态展示正确
- [ ] 6.4 前端测试补充状态文案/集数统计格式化用例，验证 `vitest` 通过

```

## docs/openspec/changes/episode-status-cache/specs/episode-cache/spec.md

- Source: docs/openspec/changes/episode-status-cache/specs/episode-cache/spec.md
- Lines: 1-41
- SHA256: 5b93c8395b28f77d686274001795de116e82779011002b07c9e5fae10621ea37

```md
## Purpose

为集数状态展示提供持久化的 TMDB 集信息数据源：将每季每集的标题、首播日期等元数据缓存入数据库，并通过每日定时任务刷新，避免每次查询实时调用 TMDB 导致的缓慢与不稳定。

## ADDED Requirements

### Requirement: TMDB 集信息入库缓存

系统 SHALL 将影视（media_type 为 tv/series）的每季每集元数据（季号、集号、集标题、首播日期）写入数据库缓存表；集信息持久化后供影视详情与集数状态展示直接读取，不实时调用 TMDB。

#### Scenario: 首次拉取集信息入库
- **WHEN** 系统为某影视首次执行集信息同步
- **THEN** 该影视各季各集元数据写入缓存表，后续展示直接读取缓存

#### Scenario: 无 TMDB 数据时降级
- **WHEN** TMDB 不可用或该影视无集信息
- **THEN** 缓存保持为空且不阻断影视详情与集数状态展示，展示层回退到现有数据源

### Requirement: 每日定时刷新集信息缓存

系统 SHALL 按可配置的刷新间隔（默认每日一次）为已缓存集信息的影视刷新 TMDB 集元数据；刷新成功后更新缓存表，失败时保留旧数据并记录日志。

#### Scenario: 到点刷新缓存
- **WHEN** 距上次集信息刷新达到配置间隔
- **THEN** 系统刷新全部已缓存影视的集元数据并更新缓存表

#### Scenario: 刷新失败保留旧数据
- **WHEN** 某影视 TMDB 刷新失败
- **THEN** 该影视缓存保留上次成功数据，本次失败写入日志且不影响其余影视刷新

### Requirement: 集信息缓存对外提供结构

系统 SHALL 通过影视详情接口输出缓存的集信息列表，每项包含季号、集号、集标题与首播日期；无缓存时该列表为空。

#### Scenario: 详情接口返回缓存集信息
- **WHEN** 用户查看影视详情且该影视已有集信息缓存
- **THEN** 接口返回带季号/集号/标题/首播日期的集信息列表

#### Scenario: 无缓存返回空列表
- **WHEN** 用户查看影视详情且该影视无集信息缓存
- **THEN** 接口返回空集信息列表，不报错

```

## docs/openspec/changes/episode-status-cache/specs/media-status/spec.md

- Source: docs/openspec/changes/episode-status-cache/specs/media-status/spec.md
- Lines: 1-79
- SHA256: b2cbd3bb64249d29f575b18bafc125686c7b140793b30c4fbc0a30304c22e237

```md
## Purpose

提供影视与单集粒度的正确状态统计与标记：影视列表「已有 x/xx 集」、影视详情「集数状态」的真实聚合；标记状态机（已在库/巡检中/异常/未开播）；单集真实文件大小；影视下载入库完成后自动更新集数状态。

## ADDED Requirements

### Requirement: 影视列表集数统计真实展示

系统 SHALL 在影视列表返回每部影视的集数统计（可用集数与总集数），其中可用集数按真实已入库状态聚合；不得因聚合逻辑缺失而恒为 0。「已有 x/xx 集」与「共 x 集」据此展示。

#### Scenario: 列表展示真实已入库集数
- **WHEN** 某剧集已有 5 集完成入库、全集 20 集
- **THEN** 列表显示「已有 5 / 20 集」，而非 0

#### Scenario: 电影集数统计
- **WHEN** 影视类型为电影
- **THEN** 列表不显示集数统计，回退到电影状态展示

### Requirement: 影视详情集数状态真实展示

系统 SHALL 在影视详情返回每集的单集状态（含状态、真实文件大小、更新时间），状态按标记状态机归一；集数状态不得因聚合逻辑缺失而恒为 0。

#### Scenario: 详情展示单集状态
- **WHEN** 用户查看某剧集详情且该剧集已有在库/巡检中的单集
- **THEN** 详情集数状态区逐集展示状态、大小与更新时间

#### Scenario: 无单集记录
- **WHEN** 某剧集详情无任何单集记录
- **THEN** 集数状态区为空但不报错，不展示错误的 0 状态

### Requirement: 标记状态机统一判定

系统 SHALL 以统一规则判定影视/单集标记状态：
- **已在库**：已入库完成（Emby 收录 / 下载完成入库）；
- **巡检中**：已加入巡检流程，正在等待或执行搜索/转存/下载；
- **异常**：已开播但未下载成功（含未搜索到可用资源）；
- **未开播**：尚未开播（TMDB 首播日期晚于当前时间或未到首播）。

判定结果 SHALL 与影视实际在库/队列/下载数据一致，并在状态变化时更新。

#### Scenario: 已在库判定
- **WHEN** 某集已入库完成
- **THEN** 该集标记为「已在库」

#### Scenario: 巡检中判定
- **WHEN** 某集存在巡检/下载队列记录且状态为进行中
- **THEN** 该集标记为「巡检中」

#### Scenario: 异常判定
- **WHEN** 某集已开播但下载失败或未搜索到资源
- **THEN** 该集标记为「异常」，用户可据此识别需要关注的任务

#### Scenario: 未开播判定
- **WHEN** 某集首播日期晚于当前时间或未到首播
- **THEN** 该集标记为「未开播」，不参与巡检/下载

### Requirement: 单集大小真实值

系统 SHALL 在影视详情集数状态与队列中展示每个单集的真实文件大小（取自对应文件记录），不得默认等同该影视第一个文件大小或任一共享占位值。

#### Scenario: 展示单集真实大小
- **WHEN** 某影视多个单集大小不同
- **THEN** 详情与队列逐集展示各自真实大小，而非全部相同

#### Scenario: 大小缺失
- **WHEN** 单集尚无文件大小记录
- **THEN** 展示为无大小（—），不填充虚假值

### Requirement: 入库完成后更新集数状态

影视下载入库完成后，系统 SHALL 自动更新该影视/单集的标记状态（置为「已在库」）与集数统计，无需人工触发。

#### Scenario: 单集入库后状态更新
- **WHEN** 某集完成下载并入库
- **THEN** 该集标记自动变为「已在库」，影视集数统计随之增加

#### Scenario: 状态更新可见
- **WHEN** 集数状态更新后用户刷新列表或详情
- **THEN** 展示的最新状态与统计反映入库结果

```
