# fix-audit-issues build 派发检查点

- plan: docs/superpowers/plans/2026-09-17-fix-audit-issues.md
- review_mode: standard（仅风险任务派发 reviewer，最多 1 轮 review-fix）
- tdd_mode: tdd（implementer 必须提供 RED/GREEN 证据）
- 当前阶段: implementing
- 已完成: Task A1（外键级联迁移，review clean + R1 修复闭环，勾选 2.1/2.2/2.3/2.5 已验证）

- 当前 task: A2（前端 MediaItem.tmdb_id 类型契约对齐）
  - brief: .superpowers/sdd/2026-09-17-fix-audit-issues/task-A2-brief.md
  - model: fixer（implementer）
  - 风险信号: 预计不命中（单文件类型放宽，非跨模块/非安全/非迁移/非 API 契约）→ 若未命中则不派 reviewer，直接勾选
  - BASE: d35ad79
  - 状态: 待派发 implementer
