# Brainstorm Summary

- Change: restore-scan-interval-scheduling
- Date: 2026-09-14

## 确认的技术方案

### 现状事实（已核实）

- `scan_all_media(force=False)`（scan.py:1497）当前无到期过滤，每轮遍历全部 tracking/downloading 影视逐一巡检；`force` 参数保留但不再改变遍历行为。
- `scan_all_media_job`（scheduler IntervalTrigger 每分钟）→ `scan_all_media()`；异常时记录 task_run(error) 兜底。
- `_scan_one`（scan.py:1585）各 `_finish` 分支 touch 语义：
  - paused/error 跳过（1634）：`touch_last_scan_at=False`（现状正确，保留）
  - Emby 故障 error（1650-1654）：`touch=True` → **False**
  - Emby 未收录 skipped（1663-1667）：`touch=True` → **False**
  - 无遗漏 skipped（1711-1714）：`touch=True`（保留——巡检正常完成）
  - 搜索异常 error（1727-1731）：`touch=True` → **False**
  - 上限读取失败 error（1743-1746）：`touch=True` → **False**
  - 正常完成 success/skipped（1936-1939）：`touch=True`（保留）
- `_finish_scan_run` 在 `touch_last_scan_at=True` 时 `update(Media).values(last_scan_at=_now())`。
- `_now()` = `app.utils.now_utc_naive`（naive UTC），与写入同源。
- `Media.scan_interval_minutes`（server_default=60）/ `last_scan_at`（DateTime）字段仍存在。
- `config.SCAN_INTERVAL_MINUTES = 60`；全局 `system_config.scan_interval_minutes` 已退休（不复活）。
- scheduler.py 模块 docstring 与 `register_jobs` docstring 已描述到期过滤行为（恢复后重新为真）；需改的是 scan.py 内部 `scan_all_media` / `scan_all_media_job` docstring 及 downloading 注释。
- force 无真实调用入口（仅测试）；**用户确认：仅恢复函数语义，不新增 CLI/API 入口**。
- 存量摸底（DB）：8 条 media 间隔全部 60/NULL，无异常小值；last_scan_at 无 NULL。

### 用户确认的决策

1. **force 范围**：仅恢复 `force=True` 绕过到期过滤的函数语义 + docstring，不新增入口。
2. **默认间隔**：60 分钟新剧发现延迟可接受（存量已摸底无异常值）。
3. **touch 分支**：全部「未真正完成主流程」短路分支改 `touch_last_scan_at=False`（Emby 故障 / 未收录 / 搜索异常 / 上限读取失败）；无遗漏与正常完成保留 True。

### 核心实现方案

**到期过滤（SQL 侧）**：`scan_all_media` 的 select(Media) 查询中，`WHERE status IN (tracking, downloading) AND (last_scan_at IS NULL OR last_scan_at <= :threshold)`；threshold 按每 media 间隔计算（`media.scan_interval_minutes ?? settings.SCAN_INTERVAL_MINUTES(60)`，`_now()` naive UTC 同源）；`force=True` 时跳过该过滤条件。

**touch 语义**：`_scan_one` 四个短路分支 `touch_last_scan_at=False`（保留日志与重试计数，下轮 tick 继续重试）。

**docstring 同步**：`scan_all_media` docstring 恢复「按 per-media 间隔到期过滤」描述并修正「force 不再改变遍历行为」过时句；`scan_all_media_job` docstring 同步；downloading 注释澄清「不排除出巡检集合（防卡死）≠ 绕过冷却」；scheduler.py 无需改（描述已为目标态）。

## 关键取舍与风险

- 新剧发现延迟：≤1min → ≤60min（默认），快速追更用户感知变化；引导调小关注剧集间隔（release note）。
- touch 语义回归：短路分支 touch=False 属行为回归 → 回归测试 + release note。
- 测试翻转范围：`test_scan_baseline.py`（within_interval_cooldown 断言「未到期也扫」）、`test_oracle_fixes.py`（scans_all_tracking / force_skips_due_filter）→ 逐用例核对翻转。
- Task 3「自然重试」依赖核实：`_scan_one` 搜索成功但无候选 → 走正常完成 touch=True → 缺集重试延迟从 ≤1min 变为 ≤interval（≤60min），符合「自然重试」语义但节奏变化；task 1.1 核实后记录结论到 design.md Open Questions 处置。
- 存量无异常小值 → 「存量间隔值爆炸」风险排除。

## 测试策略

- 新增：「未到期跳过」「last_scan_at NULL 立即扫」「force 绕过过滤」「故障/未收录下轮重试」。
- 翻转：`test_scan_baseline.py` / `test_oracle_fixes.py` 固化「未到冷却期也巡检 / force 不再改变行为」断言，逐用例核对无隐含依赖；全量回归 `cd backend && .venv/bin/python -m pytest tests/ -x -q`。

## Spec Patch

无需 patch delta spec（现有 Scenario 已覆盖全部确认行为；「搜索异常/上限读取失败 touch=False」不改变验收场景语义）。