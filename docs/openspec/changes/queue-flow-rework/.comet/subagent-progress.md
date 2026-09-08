# Subagent Progress — queue-flow-rework

- Plan task: Task 4 下载队列从 TaskQueue FIFO 取件生成 pending
- OpenSpec task: 2.3 下载队列新增「从 TaskQueue 按 FIFO（created_at,id）取尚未生成 DownloadQueue 的任务 → 生成 pending 行」
- Phase: task-review → fix round 1/1（Needs fixes：2 Important + 2 Minor 顺手项）
- Model: fixer (ses_f7df7b18fffelW9NmovsgDVKl5) resumed
- review_mode: standard（风险触发式 — 命中并发/CAS + diff>200）
- baseline: 0365bec199aac8fab3fd633cb6602ece9ae247a8
- 实现提交: aeea29d4210bbaa57258d3cbafd252448eace124
- TDD 证据: RED 2 failed → GREEN（30 passed + transfer 38 regression）
- 审查-修复轮次: 1/1 进行中
- Rulings:
  1. 「跳过行置 done」→ 改为 SQL 层 NOT EXISTS 排除，跳过行保持 ready（保重试路径 + FIFO 不饿死）
  2. 补 _admit_batch 集成测试（has_pending 早退前先取件）
  3. Minor#1 IntegrityError 防御兜底、Minor#2 or 兜底触发时 logger.warning（顺手做）
- 状态: fix round 1/1 进行中（ora-4 完成后产出的 findings 修复）