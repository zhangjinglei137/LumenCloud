# Subagent 进度检查点 — queue-inspection-rework

> Comet 协调检查点（持久事实来源：Comet workflow 状态 + plan/OpenSpec checkbox + 本文件）
> plan: docs/superpowers/plans/2026-09-10-queue-inspection-rework.md
> review_mode: standard（仅风险任务派发每任务 reviewer，最多 1 轮 review-fix）
> tdd_mode: tdd

## Task 2 — size_estimated 落库 + aria2 真实大小回填（数据链路）

- 状态：`task-review`（fix-3 回报 DONE_WITH_CONCERNS）
- OpenSpec 映射：tasks.md 2.1 / 2.2
- 命中风险信号：数据/schema 迁移 + 并发/锁（aria2 回填条件更新）→ 派发每任务 reviewer
- 实现提交哈希：6dfe309（base 34cf38a）
- 变更文件：迁移 0016 + models + scan.py + transfer.py + queue.py + 新测试（383+/20-）
- RED/GREEN 证据：定向 7 passed（RED 首跑 7 failed）；回归 64 passed + 补跑 118 passed；全量 485 passed
- 审查-修复轮次：0/1（进行中，ora-3）
- 顾虑/裁定记录：
  - **Ruling（fix-3 报告，协调者认可）**：brief 写 `down_revision="0015"`，实际 revision id 为 `"0015_episode_info_cache"` → implementer 已按实际 id 实现并真跑 upgrade head/downgrade 冒烟验证（含全链 16 个迁移）。plan brief 的 down_revision 需修正（小偏差，不阻断）
  - real_size=None 条件构造 values 规避 file_size NOT NULL 写 NULL → 正确
  - trigger_download_complete 事务内新增 tell_status RPC，aria2 故障静默不回填 → 符合设计

## 已完成任务归档（摘要）

- Task 1 ✅：实现 27edc97 → 勾选 7f3e254；ora-1 spec-✅（0/0/4 Minor）
- Task 1b ✅：实现 9cc82c8 → 勾选 34cf38a；ora-2 spec-✅（0/0/3 Minor）；BLOCKED 一次为范围扩展

## 待处理事项（deferred minors / 跟进）

1. test_api_smoke 与 test_queue_list 共享模块级 engine 的顺序依赖（预存在基建，已局部规避）→ Verify 前评估，倾向不动
2. `_list_download` 活跃态过滤语义：前端下载 Tab「仅看活跃」开关 → Task 4 派发时确认
3. test_queue_list.py 结尾无换行、test_queue.py:242 用例名语义偏差 → deferred，Verify 前统一
4. plan Task 2 brief 的 down_revision 偏差 → 勾选 Task 2 时同步修正 plan 文本（写 "0015_episode_info_cache"）