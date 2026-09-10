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