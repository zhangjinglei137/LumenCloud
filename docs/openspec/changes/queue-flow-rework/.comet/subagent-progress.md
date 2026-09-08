# Subagent Progress — queue-flow-rework

- Plan task: Task 6 事件触发下载队列消费 + 容量释放续跑
- OpenSpec task: 3.2 巡检入队 + 下载完成两处事件触发「下载队列消费尝试」；3.3 下载完成释放容量后自动续跑等待队列
- Phase: implementing
- Model: fixer (ses_f7de19242ffeuWuEkMEOJXypkh)
- review_mode: standard（风险触发式）
- baseline: 74f1feabb463258404f31d4051c611619dcff07e
- 实现提交: pending
- 审查-修复轮次: 0/1
- 状态: implementing
- 前置任务: Task 1-5 ✅
- 衔接: trigger_transfer_consume() = _fetch_from_task_queue + _admit_batch 有界循环；scan enqueue 成功尾部 + library done 路径触发；每分钟 job 兜底保留；asyncio.Lock 防重入