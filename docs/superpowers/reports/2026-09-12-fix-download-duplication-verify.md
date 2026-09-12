# 验证报告：fix-download-duplication（2026-09-12）

## 验证模式

轻量验证（light，手动覆盖——实现改动 2 文件、3 任务、无 delta spec；
自动评估因计入 Comet change 自身 12 个产物文件而误判为 full）。

## 轻量验证 6 项检查

| # | 检查项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | tasks.md 全部任务已完成 | ✅ PASS | 3/3 任务 `[x]` |
| 2 | 改动文件与 tasks.md 描述一致 | ✅ PASS | `backend/app/tasks/scan.py`（_enqueue 跨键防重）+ `backend/tests/test_scan_enqueue_dedup.py`（新增回归测试），与 Task 1/2 描述吻合 |
| 3 | 编译通过 | ✅ PASS | `py_compile app/tasks/scan.py` → COMPILE_OK |
| 4 | 相关测试通过 | ✅ PASS | scan 相关 66 通过；全量 592 passed（record-check verify exit=0） |
| 5 | 无明显安全问题 | ✅ PASS | diff 扫描无硬编码密钥/新增 unsafe 操作（stoken/fid_tokens 均为测试占位 None） |
| 6 | 集成代码审查 | ✅ SKIP（记录原因） | `review_mode: off`（hotfix 预设），改动仅 _enqueue 幂等检查新增 13 行 + 测试，无正确性/安全/边界风险 |

## 根因消除确认

- proposal 根因：全量模式（Emby 未收录）用文件名键 vs 标准模式 SxxExx 键，
  绕过 `UNIQUE(media_id, episode)` 重复入队。
- 修复：`_enqueue` 新增「同 media 同 file_name」跨键防重——同一物理文件无论
  以哪种键入队，第二次一律 existing。
- RED 验证：修复前 `test_enqueue_cross_key_same_file_returns_existing` 失败
  （返回 enqueued）；修复后 5 个新测试全部通过。

## 实现偏差记录

design.md/tasks.md 初稿方案（全量模式「第N集」归一化为 SxxExx）在 build 阶段
发现缺陷：全量模式无 Emby 基线、无 missing_keys，**无法获取季号信息**，无法可靠
归一化（多季剧会键错乱）。实际采用 `_enqueue` 跨键防重方案（根因消除），
design.md/tasks.md 已同步更新记录该决策。

## 结论

6 项检查全部 PASS（第 6 项按 review_mode: off 记录跳过原因）。
无 CRITICAL / IMPORTANT 问题。验证通过，可进入归档。

## 遗留事项

- 生产库「渗透」10 集双键重复记录需发布后人工清理（tasks.md「生产数据人工清理」）。
