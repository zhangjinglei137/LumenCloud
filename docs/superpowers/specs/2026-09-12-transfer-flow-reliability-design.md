---
comet_change: fix-transfer-flow-reliability
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-12-fix-transfer-flow-reliability
status: final
---

# 转存下载流程可靠性修复 — 深度技术设计

> 上游事实源：`docs/openspec/changes/fix-transfer-flow-reliability/`（proposal.md / design.md / specs/）。本文件是对 open 阶段高层决策（D1-D8）的深度技术细化：实现方案、边界条件、测试策略与技术风险。

## 1. 实现环境约束（设计前提）

- 单 worker + SQLite（`StaticPool` 单连接，`busy_timeout=5000ms`）；`database.py` 已声明「网络 IO 不放事务内」约定，但 `_try_admit_one` 准入段违反之。
- 准入原子性三层：进程锁 `_admission_lock` → 事务级锁行（`system_config` 锁行写触发 SQLite 排他写锁）→ 行级 CAS（`WHERE status='...'`）。
- reserved 容量账本 = DB 聚合 `transferring/scrape/library` 的 `file_size` SUM；`downloading` 被排除（依赖 used 覆盖），used 有 30s 进程内缓存。
- 状态机 CAS 幂等协议（`_node_failure`/`_complete_download`/`_commit_downloading` 的 retry_count/node_attempt 双快照）经评审确认正确，不得破坏。

## 2. 实现设计（按任务组细化）

### T1 P0-1 准入事务边界重构（transfer.py + capacity.py）

**现状**：`_try_admit_one`（transfer.py L1153-1182）在 `async with s.begin()` 内锁行后调用 `capacity.provider.check(reserved + file_size)`，其内部 `get_usage()` 缓存未命中时递归 `alist.list_dir(/quark)`（网络 IO），并调 `_persist_snapshot` 新开 session commit。

**改造**：准入段拆为三段：

```
短事务A（只读）: SELECT pending 行快照（enqueued_at,id FIFO 取 1 行，detached）
锁外:           _preflight_quark_mount（纯网络/DB 短事务混合，保持现状）
锁外:           capacity.provider.check(reserved + file_size)   ← 网络 IO + 快照落库都在无外层事务上下文
短事务B（写）:  锁行 → SELECT SUM(reserved) → CAS pending→transferring（或置 quota_wait）
```

**要点**：
- reserved 读仍必须在事务 B 内（锁行后重读，读到最新已提交 in-flight），`_read_reserved_in_tx` 保持。
- `_persist_snapshot` 在锁外 check 时自然脱离外层事务；`_load_quota_gb`/`_load_margin_gb` 的独立短 session 不再嵌套。
- `_admission_lock` 保持包住「锁外 check + 事务 B」两段（check 结果与 CAS 之间的竞态由事务 B 内重读 reserved 兜底——check 只是预判，事务 B 的 CAS 前重新校验，但**不重复网络 IO**：事务 B 内用「读 reserved + 比较已缓存 usage」做最终判定）。

**边界**：check 通过但事务 B 内 reserved 已增大导致超容量 → CAS 前用 `usage.used_gb + reserved_新 + file ≤ quota` 复判，不满足则置 quota_wait（不重复调 get_usage，避免事务内网络 IO 回潮）。

**验证**：新增 `test_admission_tx_boundary.py`——monkeypatch `capacity.get_usage` 断言其调用时无活动 DB 事务（检查 `session.in_transaction()` 为 False）；断言快照落库独立提交；CAS 冲突/容量不足分支回归。

### T2 P1-2 GID 白名单逃生通道（transfer.py + recovery.py）

**现状**：`_admit_batch` 段 2 白名单只收 `status='downloading' AND aria2_gid 非空`（L1481-1500）；recovery 回退 downloading→pending 时若 `aria2.remove` 失败，孤儿 gid 永不在白名单 → 每轮整批跳过，自锁。

**改造**（双层）：
1. 白名单口径放宽：`SELECT aria2_gid WHERE aria2_gid IS NOT NULL`（不限 status）——回退中/在库任务不再误判陌生。
2. 哨兵清理：新增模块级 `_unknown_gid_strikes: dict[str, int]`（进程内）；白名单未命中的 gid 每轮 `strikes += 1`，`≥ _GID_STRIKE_LIMIT(3)` 时执行一次 `aria2.client.remove(gid)` best-effort + `_record_alert`，然后清计数字典条目（防重复删除）；陌生 gid 自然消失（终态）后从 dict 移除。

**边界**：仅对「不在 DB 任何行」的 gid 计数；本系统任务在库有行不受影响；删除前告警可追溯。

**验证**：新增 `test_gid_escape.py`——孤儿 gid 连续 3 轮后触发 remove；在库 gid 不触发；外部 paused 任务可被哨兵解除阻断。

### T3 P1-1/P1-3 webhook 文件级推进与节点重置（nastools_notify.py）

**现状**：`_advance_scrape_to_library(media_id)` 按 `WHERE media_id=? AND status='scrape'` 批量推进全部行；只设 status/node_started_at/updated_at。

**改造**：
- 解析 webhook 载荷中的文件名/集号（`SxxExx` / `第N集` / 文件名），与 scrape 行的 `file_name`/`download_name`/`episode` 匹配，CAS 单行推进 `WHERE id=? AND status='scrape'`。
- 无法定位时：不推进任何行，仅调用 `trigger library_check 轮询加速`。
- 推进 UPDATE 补齐 `node_attempt=0, node_finished_at=now, node_error=None`。

**边界**：载荷无文件名（旧版 NaSTools webhook）→ 退化仅触发轮询；同 media 多集 scrape 时只推进匹配行。

**验证**：更新 `test_nastools_webhook.py`——单文件事件只推进匹配行、其余保持 scrape；节点字段重置断言；无法定位时零推进 + 轮询触发。

### T4 P1-4 cancel/skip 同步 media 状态（routers/queue.py）

**改造**：`cancel_task`（L361-374）/ `skip_task`（L459-472）置 DQ 终态（failed/skipped）后，同一事务内调用 `_sync_media_status(media_id, session)`（transfer 既有 helper：media 无在途任务时回落 tracking）。

**边界**：仅当该 media 已无任何 `_ACTIVE_STATUSES` 任务时才回落；cancel 后触发转存消费（释放容量）保持现状。

**验证**：更新 `test_queue.py`——取消最后任务后 media.status 回落 tracking；多任务在途时保持 downloading。

### T5 P1-5 转存提交冲突清理夸克残留（transfer.py）

**改造**：`_transfer_chain` 将 `final_quark_path` 作为参数传入 `_commit_downloading`；`_DownloadStateChanged` 分支在 `aria2.remove` 之外追加 `await alist.remove(拆分后的 final_quark_path)`（复用 `_split_quark_path` + `alist.remove(names, dir_part)`），失败仅告警。

**边界**：CAS 失败时新名未落库、旧快照 quark_path 是旧值——`final_quark_path` 是转存链内存中最新值，直接使用；若为空回退 `/quark/{file_name}`。

**验证**：新增测试——monkeypatch `_commit_downloading` 抛 `_DownloadStateChanged`，断言 alist.remove 被调用且参数为 final 路径。

### T6 P1-6 取件-创建原子化（transfer.py）

**现状**：`_fetch_from_task_queue`（L1322-1360）先 CAS `ready→done`，再保存点 INSERT DQ；撞 UNIQUE 时保存点回滚但 done 已在外层事务提交。

**改造**：单语句条件 INSERT：

```sql
INSERT INTO download_queue (media_id, episode, file_name, ..., status='pending')
SELECT :media_id, :episode, ... FROM task_queue tq
WHERE tq.id = :tq_id AND tq.status = 'ready'
  AND NOT EXISTS (SELECT 1 FROM download_queue WHERE media_id=:media_id AND episode=:episode)
```

同事务内对命中行 `UPDATE task_queue SET status='done'`；INSERT 影响行数 0（撞 UNIQUE 或源行已被取）→ 该行 done 不更新（保持 ready 由下轮或并发路径处理）。

**边界**：SQLite 与 Postgres 均支持该语法；取件上限 `num=10` 保持。

**验证**：更新 `test_transfer_queue.py`——并发先建 DQ 时源行保持 ready，不误标 done。

### T7 P1-7 容量记账漏计窗口（capacity.py + transfer.py）

**改造**：`CapacityProvider` 新增 `invalidate_usage_cache()`（置 `_usage_cache=None`）；`_commit_downloading` 成功后调用（文件已落盘 downloading，立即使下轮准入反映真实 used）。

**边界**：downloading 仍不计入 reserved（防双计）；仅缓存失效，不改变 used 计算逻辑。

**验证**：新增测试——准入提交后 `get_usage` 下次调用重新统计（monkeypatch list_dir 计数断言）。

### T8 P2 有界性修复（分散模块）

| 子项 | 改造 |
|---|---|
| 8.1 刮削连坐风暴 | `scrape_runner`（library_check.py）失败后对该 media 记录进程内退避时间戳（10min 内不再触发 force sync）；同步失败不批量累加全部 scrape 行 node_attempt（仅对「本 media 本轮尝试的那个任务」计数或交由 recover） |
| 8.2 aria2 options | `aria2.add_uri` options 追加 `allow-overwrite=true, auto-file-renaming=false`；实施前核对生产 aria2 启动参数，已配置则跳过（记录） |
| 8.3 集级确认 fail-open | `_episode_in_missing` 遗漏集为空时不再直接返回 False：增加一轮延迟复核标记（进程内 recent-empty-check dict，间隔 ≥60s 才放行） |
| 8.4 通知节流 | `nastools_sync` 失败通知复用 `_alert_cooldown` 模式（模块级 dict + TTL） |
| 8.5 quota_wait 写放大 | 唤醒后先 `capacity.get_usage` 算余量，余量为 0 直接返回（不进入准入循环），减少 N×UPDATE+容量查询 |
| 8.6 凭据完整性 | 取件时校验 file_name/file_size/share_code 非空，缺失保持源行 ready + 告警 |
| 8.7 save_task_id 条件化 | `_node_failure` 无条件清空改为 `WHERE status != 'transferring'` |
| 8.8 全量模式防重 | `_enqueue` 对 download_queue 补集号归一化查询（同 SxxExx 键存在则跳过） |
| 8.9 token query | `nastools_notify` 移除 `?token=` query 校验通道，仅保留 header（X-Nastools-Token）；实施前确认是否有部署依赖 query 通道 |

## 3. 测试策略

- 每个 T 组配套 `backend/tests/test_*.py` 回归测试（上述「验证」节已逐一列出断言方式），mock 外部服务（aria2/cloudsaver/alist/nastools/emby）保持既有测试风格。
- 全量 `pytest backend/tests/` 通过为 build→verify 前置条件。
- 回归重点：状态机 CAS 幂等协议（既有 `test_*_fixes.py` / `test_p1_fixes.py` / `test_oracle_fixes.py` 等全绿）。

## 4. 技术风险与缓解

| 风险 | 缓解 |
|---|---|
| D1 拆事务后 check 与 CAS 间容量竞态 | 事务 B 内重读 reserved + 内存 usage 复判，不重复网络 IO |
| 哨兵误删正常任务 | 仅对不在库 gid + 连续 3 轮 + 删除前告警 |
| webhook 文件名匹配失败率高 | 降级仅触发轮询，由既有轮询路径推进，不丢任务 |
| 进程内退避/节流重启即失 | 影响有限（重启后至多多触发一次重试）；长期选项落库（另立 change） |
| 8.2/8.9 依赖生产环境确认 | 实施前核对 aria2 参数与 NaSTools 部署，无法确认则跳过并记录（不阻塞主修复） |

## 处置记录（2026-09-12 build 完成）

> 本节记录 change 各任务实施完成后，对「4. 技术风险与缓解」表及 Open Questions 中生产确认项的逐条复查结果。结论类别：已处置 / 未确认（另立生产核对项）/ 已确认（设计决策）/ 已移交新 change。

| # | 事项（来源） | 处置结论 |
|---|---|---|
| 1 | 8.2 aria2 启动参数（Task 12） | **未确认，另立生产核对项**：仓库内无法确认生产 aria2 启动参数（docker-compose / systemd / .env 均无）；实现已照做——RPC options 与命令行参数取并集语义，重复声明无害。 |
| 2 | 8.9 NaSTools query 通道（Task 19） | **已处置**：仓库内无部署依赖证据，`?token=` 通道已移除（仅保留 header 鉴权）；docstring 记录「旧版插件需升级新版 Webhook 渠道」假设。生产插件版本另核。 |
| 3 | 单 worker 假设（Task 10/14 相关） | **假设成立，部署文档明确**：全 change 保持单 worker 假设；多 worker 部署下缓存失效（Task 10）与通知节流（Task 14）不跨进程共享。 |
| 4 | Emby 文件大小字段（Task 13） | **已处置**：已核实 emby.py 查询无 MediaSources 大小字段；电影大小校验降级为「0 字节本地下限拦截 + 告警」。 |
| 5 | 0 字节 movie 触发频率（Task 13 ⚠️） | **未确认**：admission 层对 movie 的 file_size 写入策略未在生产验证。 |
| 6 | aria2 1.36.0 RPC 契约实地行为（Task 12 ⚠️） | **未确认**：`allow-overwrite` / `auto-file-renaming` 键名与官方文档一致，但服务端实地生效需生产验证。 |
| 7 | NaSTools 载荷键名（Task 5 ⚠️） | **未确认**：`data.file_name` / `data.name` / `media_info.file_name` 三键兼容提取，但生产载荷样本未核对。 |
| 8 | aria2 gid 契约（Task 4 ⚠️） | **未确认**：`tell_active` / `tell_waiting` 返回项必含 gid 字段为运维确认项。 |
| 9 | skip 探测视图补写 skipped 不触发 media 同步（Task 7 ⚠️） | **已确认（设计决策）**：design T4 明确「仅 dq 分支」为有意决策。 |
| 10 | Task 3 自然重试对每分钟全量的依赖（本 change 相关） | **已移交新 change**：由 restore-scan-interval-scheduling change 另行承接（新 change 的 Task 1.1 事实核查），此处不再展开。 |

## 5. 非目标（明确不做）

- 不改主链路状态机拓扑与 CAS 幂等协议。
- 不新增 public API / schema / capability。
- 不切换存储引擎、不引入 Redis（节流落库为长期选项）。
- 不改前端（QueueView 轮询逻辑）。
- cloudSaver `receiveCode=stoken` 契约实测属评审遗留项，不属本 change。
