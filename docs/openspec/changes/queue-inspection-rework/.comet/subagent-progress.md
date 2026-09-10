# Subagent 进度检查点 — queue-inspection-rework

> Comet 协调检查点（持久事实来源：Comet workflow 状态 + plan/OpenSpec checkbox + 本文件）
> plan: docs/superpowers/plans/2026-09-10-queue-inspection-rework.md
> review_mode: standard（仅风险任务派发每任务 reviewer，最多 1 轮 review-fix）
> tdd_mode: tdd

## Task 3 — 入库确认卡死修复（library_check.py）

- 状态：`task-review`（fix-4 回报 DONE_WITH_CONCERNS）
- OpenSpec 映射：tasks.md 3.1 / 3.2 / 3.3
- 命中风险信号：并发/锁（CAS 条件更新）+ 状态机变更 → 派发每任务 reviewer（ora-4）
- 实现提交哈希：0c657f5（base 8f971c9）
- 变更文件：library_check.py（cause 参数 + 两路径纳入超时 + 日志）、test_library_check_timeout.py（新建 6 用例）、test_library_check.py（消息断言同步）
- RED/GREEN 证据：RED `2 failed, 4 passed`（两路径超时置 failed 断言失败，精确复现卡死根因）→ GREEN 定向 26 passed / 回归 70 passed / 全量 491 passed（2 warnings 为环境弃用告警）
- 审查-修复轮次：0/1（进行中）
- 顾虑/裁定记录：
  - node_error 文案变更（「刮削配置」→「刮削/收录配置」）→ 告警规则匹配需留意，Verify 时检查有无外部匹配
  - 缺 tmdb_id 路径由每轮 warning 改为静默等待/失败才记录 → 按 brief 执行，接受
  - TDD 技能不在 fixer 可用列表 → 按任务内 TDD 硬约束执行（RED 先行有记录），接受

## 已完成任务归档（摘要）

- Task 1 ✅：实现 27edc97 → 勾选 7f3e254；ora-1 spec-✅（0/0/4 Minor）
- Task 1b ✅：实现 9cc82c8 → 勾选 34cf38a；ora-2 spec-✅（0/0/3 Minor）
- Task 2 ✅：实现 6dfe309 → 勾选 8f971c9；ora-3 spec-✅（0/0/2 Minor）

## 待处理事项（deferred minors / 跟进）

1. test_api_smoke 与 test_queue_list 共享模块级 engine 顺序依赖 → Verify 前评估，倾向不动
2. `_list_download` 活跃态过滤语义：前端「仅看活跃」开关 → Task 4 派发时确认
3. test_queue_list.py 结尾无换行、test_queue.py:242 用例名语义偏差 → Verify 前统一
4. skip 端点漏拷 size_estimated（ora-3 M1）→ Verify 前评估
5. node_error 文案变更外部告警匹配 → Verify 时检查