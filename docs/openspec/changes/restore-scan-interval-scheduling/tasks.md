## 1. 事实核查

- [ ] 1.1 核实 Task 3「自然重试」闭环对每分钟全量的代码依赖：审查 `_scan_one` 搜索/重试路径，确认 per-media 间隔下缺失集重试延迟为 ≤interval 是否符合既有语义；将核查结论记录到本 change 的 design.md Open Questions（无代码改动）
- [ ] 1.2 摸底存量 `scan_interval_minutes` 值分布：查询 media 表中非 NULL 且 ≠60 的间隔值，评估恢复过滤后的行为回归面；异常小值（如 <5）上报用户确认

## 2. 核心实现

- [ ] 2.1 `scan_all_media` 恢复 SQL 侧到期过滤：查询按 `last_scan_at IS NULL OR last_scan_at <= :threshold` 过滤（threshold 按各 media 间隔计算，`_now()` naive UTC 同源），间隔取值链 `media.scan_interval_minutes ?? settings.SCAN_INTERVAL_MINUTES(60)`；并验证新增「未到期跳过」「last_scan_at NULL 立即扫」测试通过
- [ ] 2.2 恢复 `force` 语义：`force=True` 绕过到期过滤（CLI/手动全量入口），同步修正 `scan_all_media` docstring 中「force 不再改变遍历行为」的过时描述；并验证新增「force 绕过过滤」测试通过
- [ ] 2.3 修复 `_scan_one` 短路分支 touch 语义：逐分支审查 `_finish` 调用点，Emby 故障 / 未收录等「未真正完成主流程」的分支 `touch_last_scan_at=False`（保留日志与重试计数），仅正常完成分支 touch=True；并验证新增「故障/未收录下轮重试」测试通过
- [ ] 2.4 澄清 downloading 语义注释：`scan_all_media` 注释明确「downloading 不排除出巡检集合（防卡死）≠ 绕过冷却」，并同步 `scheduler.py` 模块 docstring 与 `register_jobs` docstring 至与实现一致

## 3. 测试同步

- [ ] 3.1 翻转固化「未到冷却期也巡检 / force 不再改变行为」的既有断言（`test_scan_baseline.py` / `test_oracle_fixes.py`）为到期过滤语义，逐用例核对无其它隐含依赖；并验证两个测试文件全绿
- [ ] 3.2 全量回归：运行 `cd backend && .venv/bin/python -m pytest tests/ -x -q`，确认既有全部测试（含巡检相关与转存相关）通过，无跨模块破坏

## 4. 文档与交付

- [ ] 4.1 在 `docs/superpowers/specs/2026-09-09-queue-flow-rework-design.md` 批注「偏离设计：未实现全局间隔，改回 per-media 到期过滤（本 change 实施）」，记录决策依据；并确认本 change 的 design.md Open Questions 处置完成
- [ ] 4.2 验收确认：修改某剧详情页间隔为 5 分钟并保存 → 5 分钟内该剧被扫（前端输入框重新生效）；观察一天 task_run 写入量回落至约 N×24；向用户说明新剧发现延迟变化（release note 项）
