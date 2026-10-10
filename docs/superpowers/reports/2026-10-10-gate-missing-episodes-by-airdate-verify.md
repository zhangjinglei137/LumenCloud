# 验证报告：gate-missing-episodes-by-airdate

- change: gate-missing-episodes-by-airdate
- verify_mode: full（任务 10 / delta 能力 1 / 变更文件 34，超阈值）
- base-ref: 03bd858
- HEAD: 3b47ec2
- verify_result: pass
- 验证语言: zh-CN

## Summary Scorecard

| Dimension | Status |
|---|---|
| Completeness | tasks.md 10/10 完成；delta spec 1 个需求（MODIFIED）+ 8 场景 |
| Correctness | 8/8 spec 场景有实现与测试证据；回归 826 passed |
| Coherence | 遵循 design.md 高层决策与 Design Doc D1–D5；无阻塞性偏差 |

## 检查项（full 模式 7 项）

| # | 检查项 | 结果 | 证据 |
|---|---|---|---|
| 1 | tasks.md 全部任务已完成 | ✅ PASS | 1.1–4.2 全部 `[x]`；Superpowers plan 7/7 task 勾选 |
| 2 | 实现符合 design.md 高层设计决策 | ✅ PASS | 门控在 scan 缺失集构建层（D1）；TMDB 为主 Emby 兜底（D2）；失败哨兵 fail-safe（D3）；全量兜底门控（D4）；复用 TMDB 缓存（D5） |
| 3 | 实现符合 Design Doc | ✅ PASS | `docs/superpowers/specs/2026-10-10-gate-missing-episodes-by-airdate-design.md` 的 D1–D5、边界条件表逐条落实 |
| 4 | 能力规格场景全部通过 | ✅ PASS | 8 场景均有测试证据（见下表） |
| 5 | proposal.md 目标已满足 | ✅ PASS | 无日期/未播出不再搜索下载；已播出且库中缺失正常入队；常规 + 全量路径均门控 |
| 6 | delta spec 与 design doc 无矛盾 | ✅ PASS（1 处 MINOR，见下，已接受） | 见「偏差记录」 |
| 7 | Design Doc 可定位 | ✅ PASS | 文件存在且 `comet_change` 指向本 change |

## Spec 场景 ↔ 实现/测试覆盖

| Spec Scenario | 实现 | 测试 |
|---|---|---|
| 无播出日期的未开播集不搜索下载 | `scan._filter_aired` TMDB 无该集且 Emby 无日期 → 剔除 | `test_filter_aired_drops_no_airdate` / `test_scan_one_skips_unaired_no_date` |
| 首播日期在未来不搜索下载 | `episode_aired` → False | `test_filter_aired_tmdb_future_drops` / `test_filter_aired_embys_fallback_not_aired_drops` |
| 已播出的缺失集正常搜索 | `episode_aired` → True → 保留 | `test_scan_one_enqueues_aired` |
| 同剧部分已播、部分未播仅已播集入队 | 逐季/逐集独立判定 | `test_scan_one_same_show_three_groups_only_aired_enqueued` |
| 无日期的未开播剧在全量兜底路径同样被拦截 | 全量 `aired_by_key` 短路 + 逐文件 (s,e) 拒绝 | `test_scan_one_tv_full_mode_skips_all_unaired` / `..._gates_unaired_files` |
| 播出日期回源失败不整体停搜 | `None` 哨兵 → `failed` → fail-safe skip | `test_scan_one_skips_on_tmdb_failure` / `test_scan_one_tv_full_mode_skips_tmdb_all_eps_failure` |
| 巡检发现缺失集并搜集资料 / 缺失集搜索无结果 / 错季候选不入队（既有） | 未改动搜索/匹配/入队链 | 既有 scan 套件全绿 |

## 验证证据

- **构建/测试**：`cd backend && ./.venv/bin/python -m pytest tests/ -q` → `826 passed, 0 failed`，EXIT=0（本会话实跑）。
- **集成验证**：同剧三组集（已过 / 未来 / 无日期）执行一次 `_scan_one`，实际输出 `task_run.message=已入队 1 个资源；2 个文件未匹配。`，`task_queue=[{episode: S01E01, status: ready}]` —— 仅已播出且库中缺失的集入队。
- **最终集成代码审查**（review_mode=standard，覆盖整 change）：结论 **Ready for archive**，无 CRITICAL / 无 IMPORTANT。

## 问题与处理

### CRITICAL
无。

### IMPORTANT
无。

### MINOR / 偏差记录（已接受，不阻塞归档）

1. **全量空正片季的 fail-open 边界**：design.md 4.5 措辞「成功但 aired_set 为空 → 短路跳过」较宽；实现对「`get_tv_all_episodes` 成功但无正片季（`{}`）」采取 fail-open（继续搜索、不做 aired 过滤），仅对「有集但全部未播出（非空且全 False）」短路。经审查确认 fail-open 更安全，且「剑来式未开播剧」属非空全 False 分支已被正确拦截；既有全量回归测试以 `[]` mock 锁定该行为。**接受**，不影响核心验收。
2. **`_filter_aired` 局部 `import date` 冗余**（`scan.py`）：模块顶部已有 `datetime` 导入；纯风格，**接受/延后**。
3. **全量剔除后短路文案**：沿用「无遗漏集（Emby 基线已覆盖）」文案，未区分「被门控剔除」；UX 措辞，**延后**。
4. **三测试文件 `_fake_air_dates` helper 重复**：维护性建议，**延后**。
5. **`today = date.today()` 本地日期**：design D2 明确取舍；跨时区边界 ±1 天，**接受**。

## 最终结论

**PASS — 可进入 archive**。无 CRITICAL / IMPORTANT；5 项 MINOR 已记录并接受/延后。
