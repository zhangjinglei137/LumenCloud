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
