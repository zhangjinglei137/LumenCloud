# Subagent Progress — queue-flow-rework

- Plan task: Task 7 转存后格式化名称（下载名称后置生成 + 幂等）
- OpenSpec task: 4.1 download_name 生成时机从 scan promote 前置改为「转存链落盘成功后」格式化并保证幂等；4.2 aria2 out / quark_path / 转移 / 入库全程沿用格式化名
- Phase: implementing
- Model: fixer (ses_f7dd39803ffezHohl8NKSaFdK7)
- review_mode: standard（风险触发式）
- baseline: d8386cf960c15131c3a021eb27a42f74b317dcd4
- 实现提交: pending
- 审查-修复轮次: 0/1
- 状态: implementing
- 前置任务: Task 1-6 ✅（Task 2/4 已留 download_name 空位）
- 衔接: _format_download_name 复用既有规则；_get_link_wait_visible(rename_to=download_name) 落盘可见后 CAS 生成；重试幂等