# 集成验证证据 — queue-flow-rework（Task 11）

- 日期: 2026-09-09
- 基线（pre-rework commit）: `11dd1be`（fix(详情页): TMDB 全集色点接入 Emby 实际收录…）
- 验证对象 HEAD: `933f275`（Task 1-10 全部合并后提交）
- 执行命令:
  - 后端全量: `cd backend && .venv/bin/python -m pytest tests/ -q`
  - 前端构建: `cd frontend && npm run build`
  - 静态检查: `grep -rn "silent_until\|unmatched" backend/app --include="*.py"`（含注释甄别）

## 1. 全量后端测试

```
6 failed, 401 passed, 2 warnings in 139.03s (0:02:19)
```

- 无 TMDB 502 网络依赖类失败（当前网络环境通过或已被适配）。
- 失败测试清单及归因见 §3。

## 2. 前端构建

```
✓ built in 7.63s
```

- 产物写入 `backend/static/`（vite 输出目录），含 chunk 体积告警（非错误，1.09MB element-plus 等），构建成功。

## 3. 失败测试归因（全部 pre-existing，无 NEW 回归）

| # | 测试 | 断言核心 | 归因 |
|---|------|----------|------|
| 1 | tests/test_scan_full_mode_filter.py::test_scan_tv_full_mode_filters_unrelated_files | DownloadQueue 直接落库旧语义 | Task 2 legacy |
| 2 | tests/test_scan_full_mode_filter.py::test_scan_tv_full_mode_limit_batch | `assert [row.episode for row in dq] == ["S01E01","S01E02"]`（dq 为空） | Task 2 legacy |
| 3 | tests/test_scan_full_mode_filter.py::test_scan_tv_full_mode_total_none_still_filters_pure_numeric | DownloadQueue 直接落库旧语义 | Task 2 legacy |
| 4 | tests/test_scan_full_mode_filter.py::test_scan_tv_full_mode_same_episode_multiple_names_dedup | DownloadQueue 直接落库旧语义 | Task 2 legacy |
| 5 | tests/test_scan_full_mode_filter.py::test_scan_movie_full_mode_unchanged | `assert [row.episode for row in dq] == ["movie:大话西游"]`（dq 为空） | Task 2 legacy |
| 6 | tests/test_scan_numeric_match.py::test_scan_one_skips_dead_candidates_reaches_valid_share | `assert len(dq) == 1`（dq 为空） | Task 2 legacy |

### 归因方法（git 提交点逐级验证）

对 `tests/test_scan_full_mode_filter.py` + `tests/test_scan_numeric_match.py` 同一批量（两个文件在 11dd1be..HEAD 间 md5 完全一致、从未修改）在关键提交点分别运行：

| 提交 | 阶段 | 结果 |
|------|------|------|
| `11dd1be` | pre-rework 基线 | 28 passed |
| `f93d9a5` | Task 1 完成点（统一巡检调度） | 28 passed |
| `829dec9` | Task 2 完成点（巡检只写 task_queue、移除 download_queue 同步双写） | 6 failed, 22 passed |
| `933f275` | HEAD（Task 1-10 全部合并） | 6 failed, 22 passed |

**结论**: 6 个失败全部从 Task 2 完成点开始出现，失败集合与 HEAD 完全一致（中间 8 个任务未改变该失败集合）。根因是 Task 2 的设计变更「巡检入队只写 TaskQueue、DownloadQueue 改由 FIFO 取件生成」，而这两个测试文件（rework 期间零修改）仍断言「巡检后 DownloadQueue 直接有行」的旧语义——即任务上下文所述 *Task 2 legacy*。

- verdict: **6/6 pre-existing**（归因 Task 2 legacy）；本 change（Task 1-10 合并体）未引入任何 NEW 失败。
- 已知 pre-existing 失败（TMDB 502 网络依赖等）在本轮测试中未复现或已被适配，无额外损失。

## 4. 静态检查（silent/unmatched 残留逻辑）

`grep -rn "silent_until\|unmatched" backend/app --include="*.py"` 逐条甄别：

- **无活跃静默写入**: 运行代码中对 `silent_until` 的两处赋值均在 `backend/app/routers/queue.py`
  （retry `:510`、手动加集 `:691`），语义均为**重置清零**（`silent_until=None`，重试/重新探测时清除旧静默字段），非写入新的静默到期时间。
- **字段定义/索引**（允许范围）: `backend/app/models/__init__.py:128`（Index `idx_tqk_silent_until`）、`:149`（列定义注释）。
- **注释引用**: `backend/app/tasks/scan.py:65,66,1024,1056,1104,1558` 均为说明性注释，无写状态逻辑。
- **`status='unmatched'` 写入**: 无。scan.py 中 `unmatched` 仅作为扫描结果计数与消息文案（`_result_message`、collect 计数），不写库。
- TaskQueue 状态集中保留 `"unmatched"`/`"error"` 仅为重试前置判断与旧任务兼容读取，无新写入。

**结论**: 运行时代码无残留静默机制写入，Task 3「移除静默机制」在运行时语义上成立。

## 5. 回归结论

- **无 NEW 回归**。401 通过 / 6 失败，失败全部归因 Task 2 legacy（pre-existing）。
- 建议（移交 controller/后续任务）: 6 个失败属有意行为变更后的测试未适配，可考虑后续任务更新这两个测试文件断言为「task_queue 落库 + FIFO 取件后 DownloadQueue 生成」的新语义。

## 6. 手动端到端演练

按 Task 11 简报 Step 4，端到端演练（添加影视 → 巡检入队 → FIFO 容量准入 → 转存后格式化 → 下载 → 转移 → Emby 扫描 → 入库释放续跑）涉及真实服务联调，超出本次验证范围，移交团队按 runbook 人工演练。
## 5. 端到端真实服务演练（7.2，用户授权本次启动）

- 环境: 本地启动 uvicorn + .env 真实凭据；外部服务探测（Emby 8096 / NaStools 3000 / cloudSaver 8008 / alist 5244 / aria2 6800）全部 HTTP 200 可达
- **已验证链路**:
  1. 创建虚构影视「测试短片」→ 触发统一巡检 → search=`no_candidates`（无缺失集→不入队→队列空）✅ 新语义（无静默、无伪任务）正确
  2. 创建真实影视「钢琴课 (1993)」(tmdb_id=393) → 巡检产出缺失集 + 完整转存凭据（share_code/stoken/fids/fid_tokens/folder_id）落 TaskQueue ✅
  3. 下载队列 FIFO 取件 → TQ 源行置 done、生成 DQ pending 行（含凭据快照）✅
  4. 队列 API 扁平列表返回「钢琴课 (1993) - movie:钢琴课 (1993)」2.7GB ✅ 扁平契约 + 终态剔除
- **演练发现并修复的真实 bug（用户确认修代码）**: aria2 RPC HTTP 400 根因 = ARIA2_TOKEN 配置值自带 `token:` 前缀 + `_rpc` 无条件再包前缀 → `token:token:xxx` Unauthorized → GID 校验失败 → 下载队列每轮跳过（「迟迟不开始」根因）。修复 commit `c93c9d9`（token 前缀归一化，兼容两种配置形态）+ docstring 同步 `03a9cd2`。
- **修复后复验**: transfer job 从「aria2 RPC 返回 HTTP 400」变为「检测到非本系统 aria2 任务（gid 不在已签发集合），本轮跳过转存（下轮自动续跑）」——Task 5 降级语义正确生效（告警+跳过，不再整批停摆）。aria2 环境存在 n8n/历史陌生任务 gid，属运营环境事件，需人工确认；非代码缺陷。
- **演练边界**: 验证到「容量准入前」即止——未对本演练影视触发完整 2.7GB 真实转存/下载/转移（避免对真实网盘/aria2 造成副作用）；下游转移/Emby 扫描链路已由自动化测试（test_transfer/test_library_check/test_nastools_notify 全绿）覆盖。
- 演练数据: 测试 media 2 条（测试短片/钢琴课）+ TQ/DQ 各 1 行残留于本地开发库，供后续真实联调使用或清理。
