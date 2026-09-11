# Comet Design Handoff

- Change: queue-size-and-progress
- Phase: design
- Mode: compact
- Context hash: f61a47792ef813082a273758bf294aaf3ac6246db88e6216cfd8b558f753e89f

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/queue-size-and-progress/proposal.md

- Source: docs/openspec/changes/queue-size-and-progress/proposal.md
- Lines: 1-24
- SHA256: 4c4f9965106f8ff8a21d1931d098e19d509c315430c04217bb1660d1b9babfe4

```md
# Proposal: queue-size-and-progress

## Why

巡检队列每行大小显示「约 xxGB」（`size_estimated` 估算值加「约」前缀），用户要求显示精确大小（可从接口获取真实文件大小）；下载队列中进度条与文件大小混排在同一个单元格内展示，信息层级不清晰，需要分开展示。

## What Changes

- **巡检队列大小显示精确值**：去掉「约」前缀展示；精确大小优先取自真实文件大小来源（转存/下载记录、cloudSaver 分享接口返回的文件大小、aria2 tell_status），仅当无法获取时回退估算值并在 UI 标注
- **下载队列进度与大小分开展示**：进度（进度条 + 速度）与文件大小拆分为独立展示区域/单元格，避免混排

## Capabilities

### New Capabilities
<!-- 无新增 capability -->

### Modified Capabilities
- `queue-inspection-display`: 队列大小真实值（精确大小展示优先、估算仅兜底）；下载队列展示字段（进度与大小分开展示）

## Impact

- 后端：`backend/app/routers/queue.py`（下载/巡检列表接口大小字段来源）、`backend/app/services/cloudsaver.py`（分享接口精确文件大小获取）、`backend/app/tasks/scan.py`（大小估算回填逻辑核查）
- 前端：`frontend/src/views/QueueView.vue`（大小列、下载队列进度与大小分区展示）、`frontend/src/utils/format.ts`（formatFileSize 语义调整）
- 依赖：无新依赖；无数据库变更

```

## docs/openspec/changes/queue-size-and-progress/design.md

- Source: docs/openspec/changes/queue-size-and-progress/design.md
- Lines: 1-38
- SHA256: 56d2383b983b09bf4f050871216ddbd46c046a843a62e4fd8a8f1f5f778421cd

```md
## Context

巡检队列大小列由 `QueueView.vue` 的 `formatFileSize(row.file_size, row.size_estimated)` 渲染，`size_estimated` 为真时显示「约 xxGB」；该估算值来自 `_enqueue` 阶段 cloudSaver 分享均摊，用户要求显示精确值（可从接口获取文件大小）。下载队列下载中单元格混合了进度条 + 速度 + 大小，需要拆分。后端 `/queue/download/progress` 已提供 aria2 聚合的 progress/speed；`file_size` 为字节级字段。见 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 巡检/下载队列大小改以精确值优先；「约」仅作为兜底标注
- 下载队列进度与大小分区展示

**Non-Goals:**
- 不改队列分页/筛选/任务流转
- 不引入新的存储字段（沿用 file_size/size_estimated，仅在取值来源上做精确回填）

## Decisions

**D1: 精确大小来源优先级：转存/下载文件真实大小 > 探测接口返回大小 > 估算兜底**
- 理由：下载链路能拿到真实文件大小，探测链路（cloudSaver 分享）也返回文件大小字段但当前未接入
- 实现：在列表组装时尝试从已有记录取真实大小；探测结果中若携带文件大小则直接使用，否则标记 estimated

**D2: 前端 formatFileSize 语义调整**
- `formatFileSize(size, estimated)` 的「约」前缀仅当 estimated=true 且无真实值时渲染；伴随后端确保 default 为精确值
- 备选：前端以 estimated 判断展示 → 保持但后端填真值后 estimator 为 false，行为自然收敛

**D3: 下载队列进度与大小拆列**
- 进度（进度条 + 速度）与大小分别独立单元格列，宽度固定，避免混排

## Risks / Trade-offs

- [探测接口获取文件大小可能增耗时] → 仅对单任务展示聚合，复用既有列表查询结果，不新增逐行 N+1 调用
- [历史任务无真实大小] → 兜底估算标注，新任务从入口即填真值

## Migration Plan

- 纯展示与取值来源调整，无迁移

## Open Questions

无。
```

## docs/openspec/changes/queue-size-and-progress/tasks.md

- Source: docs/openspec/changes/queue-size-and-progress/tasks.md
- Lines: 1-17
- SHA256: 8b6828a7e1d4b6c1d226acab76f358963ee2336903cb4c8a8bfac05b5c29e476

```md
# Tasks: queue-size-and-progress

## 1. 后端：精确大小来源

- [ ] 1.1 核查队列大小来源：routes/queue.py 列表接口与 tasks/scan.py `_enqueue` 大小估算/回填逻辑，确认真实大小来源（转存/下载记录、cloudSaver 分享文件大小、aria2）与 size_estimated 判定
- [ ] 1.2 提升精确大小获取：cloudSaver 搜索/分享信息解析带出真实文件大小；下载完成后将真实大小回填 file_size 并清除估算标记，验证新任务 default 为精确值、旧任务可回填
- [ ] 1.3 补充后端测试：精确大小优先、估算兜底标注、下载后回填，验证 `pytest` 通过

## 2. 前端：大小与进度展示

- [ ] 2.1 frontend/src/utils/format.ts `formatFileSize` 语义调整：estimated=true 且无真实值时保留「约」前缀，否则显示精确值，验证调用处输出符合新语义
- [ ] 2.2 QueueView.vue 巡检队列大小列移除无条件「约」前缀，验证展示精确大小
- [ ] 2.3 QueueView.vue 下载队列拆列：进度（进度条 + 速度）与文件大小独立展示区域/单元格，验证下载中行进度与大小分区清晰、非下载中行仅大小
- [ ] 2.4 前端测试补充：formatFileSize 新语义、下载队列分区渲染，验证 `vitest` 通过

## 3. 集成验证

- [ ] 3.1 `npm run build` 通过；本地起服查看巡检/下载队列展示符合预期
```

## docs/openspec/changes/queue-size-and-progress/specs/queue-inspection-display/spec.md

- Source: docs/openspec/changes/queue-size-and-progress/specs/queue-inspection-display/spec.md
- Lines: 1-44
- SHA256: dde85051c3b0d9d2f8bd6b03ff10ce02d0fdd46d68dd92b63460ebde0420540a

```md
# queue-inspection-display Delta Spec

## MODIFIED Requirements

### Requirement: 队列大小真实值

巡检队列与下载队列的每行大小 SHALL 展示该任务真实文件大小（对应文件记录），优先展示精确值，只有完全无法获取真实大小时才回退估算值。精确大小来源包括探测接口返回的文件大小、下载/转存记录及 aria2 等实际文件大小；估算值仅在真实来源均缺失时使用并保留「约」标注。

#### Scenario: 逐行真实大小
- **WHEN** 队列中多个任务文件大小不同
- **THEN** 每行展示各自真实大小，不全部相同

#### Scenario: 精确大小优先
- **WHEN** 任务可从接口获取到真实文件大小
- **THEN** 队列行直接展示精确大小（不带「约」前缀）

#### Scenario: 大小缺失
- **WHEN** 某任务尚无文件大小记录（file_size 为空或 ≤ 0）
- **THEN** 显示「—」，不填充虚假值

#### Scenario: 估算大小标注
- **WHEN** 真实大小来源均不可获取（探测源未返回文件大小）
- **THEN** 展示估算值并带「约」前缀（如「约 1.9 GB」），视为兜底而非常态

#### Scenario: 下载后真实值回填
- **WHEN** 任务的转存下载完成，且可获取到真实文件大小
- **THEN** 队列行与集状态展示使用回填后的真实大小，不再显示估算值

## ADDED Requirements

### Requirement: 下载队列展示进度与大小

下载任务行 SHALL 独立展示下载进度（进度条 + 速度）与文件大小（精确值），二者分属不同展示区域/单元格，不混排；已完成/等待等非下载中状态仅展示大小不展示进度条。

#### Scenario: 下载中分行展示
- **WHEN** 任务处于下载中
- **THEN** 该行展示进度条与实时速度，文件大小独立展示于其旁列，信息不混排

#### Scenario: 下载中行精确大小
- **WHEN** 任务处于下载中，且 aria2 已知文件总大小（totalLength）
- **THEN** 该行大小独立展示精确值（不带「约」前缀），优先于估算 file_size

#### Scenario: 非下载中状态
- **WHEN** 任务未在下载（排队/等待/已暂停等）
- **THEN** 该行不显示进度条，仅展示文件大小（精确值优先）
```
