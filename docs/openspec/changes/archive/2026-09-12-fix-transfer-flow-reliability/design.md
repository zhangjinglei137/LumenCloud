## Context

背景见 proposal.md - Why（council 三方评审确认的缺陷清单）。当前实现约束：

- 单 worker + SQLite（`StaticPool` 单连接，`busy_timeout=5000ms`），`database.py` 注释明示「网络 IO 不放事务内」约定，但 `_try_admit_one` 准入段违反该约定。
- 准入原子性依赖三层：进程锁 `_admission_lock` → 事务级锁行（`system_config` 锁行写触发排他写锁）→ 行级 CAS（`WHERE status='...'`）。
- 状态机与 CAS 幂等协议（`_node_failure`/`_complete_download`/`_commit_downloading` 双快照）已被三方确认正确，本设计不得破坏。
- reserved 容量账本 = DB 聚合 `transferring/scrape/library` 的 `file_size` SUM；`downloading` 排除（依赖 used 覆盖），used 有 30s 进程内缓存。

## Goals / Non-Goals

**Goals:**

- 消除准入事务内网络 IO：容量 check 完全移出 DB 事务，恢复「锁外 check → 短事务 CAS」的原子段语义。
- 为 GID 白名单 fail-closed 提供可自愈的逃生通道，杜绝孤儿任务永久停摆。
- webhook 推进改为文件级（单行）语义并重置节点字段，与 scrape_runner 语义对齐。
- 补齐全量模式跨文件名同集防重、cancel/skip 的 media 状态回落、取件-创建原子化、CAS 冲突夸克残留清理。
- 修复 P2 有界性问题：刮削退避、通知节流、quota_wait 写放大、凭据校验、token 通道。

**Non-Goals:**

- 不改主链路状态机拓扑与 CAS 幂等协议。
- 不新增 public API / schema / capability（webhook 契约语义修正，非 API 变更）。
- 不切换存储引擎 / 引入外部缓存（节流状态落库，不引 Redis）。
- 不涉及前端行为变更（QueueView 轮询逻辑保持）。

## Decisions

### D1 准入段重构：容量 check 移出事务（P0-1）

**方案**：`_try_admit_one` 准入段拆为两个短事务 + 锁外容量 check：

1. 短事务 A（只读）：`SELECT` pending 行快照（现有取件逻辑）。
2. 锁外：`capacity.provider.check(reserved + file_size)` —— `get_usage` 内部网络 IO 与快照落库都在无 DB 事务上下文执行（`_persist_snapshot` 自身短事务不再嵌套）。
3. 短事务 B（写）：锁行 → 读 reserved → 容量 OK 则 CAS `pending→transferring`；容量不足则 `quota_wait`。CAS 行级条件更新保持并发安全，进程锁 `_admission_lock` 继续串行化准入段。

**替代方案**：check 内禁用快照落库 + 事务内 check。否决——网络 IO 时长仍阻塞单连接，且禁用快照损失可观测性，未根治问题。

**边界**：reserved 读与容量 check 之间的「读-判-抢」窗口仍由事务 B 内「锁行后重读 reserved + CAS」兜底，容量账本不因并发准入超发（事务 B 内 reserved 是最新已提交 in-flight）。

### D2 GID 白名单逃生通道（P1-2）

**方案**：双层口径 + 哨兵清理：

- 白名单放宽为「DB 中 `aria2_gid` 非空全部行」（不再限定 status=downloading）——回退 pending/transferred 但 gid 仍在库的任务不再误判陌生。
- 对仍无法匹配的陌生 gid，记录连续跳过计数（进程内 dict，key=gid）；连续跳过 ≥ N 轮（建议 3 轮）时执行一次 `aria2.remove(gid)` best-effort 并告警，打破自锁。

**替代方案**：仅放宽白名单。否决——recovery 清理失败遗留的孤儿 gid 若在库内无行仍会自锁，需主动清理哨兵。

### D3 webhook 文件级推进 + 节点字段重置（P1-1/P1-3）

**方案**：`nastools_notify._advance_scrape_to_library` 改为：

- 优先从 webhook 载荷提取文件名/集号（`SxxExx`/`第N集`）定位单行；无法定位时仅 `trigger library_check 轮询加速`，不做批量推进。
- 推进 UPDATE 补齐 `node_attempt=0, node_finished_at=now, node_error=None`，与 `_scrape_impl` 语义一致。

**替代方案**：保留批量推进仅加字段重置。否决——单文件事件批量推进未完成集直接进 library 会导致 600s 超时 failed，需按文件级。

### D4 取件-创建原子化（P1-6）

**方案**：`_fetch_from_task_queue` 的「CAS ready→done + INSERT DQ」改为单语句原子：`INSERT INTO download_queue(...) SELECT ... WHERE NOT EXISTS (同键 DQ)`，CAS 与 INSERT 在同一事务内，INSERT 未生效（撞 UNIQUE）则事务回滚连 done 更新一并回滚。

**替代方案**：保存点回滚。否决——SQLite 保存点语义脆弱，单语句条件 INSERT 更稳。

### D5 转存提交冲突清理夸克残留（P1-5）

**方案**：`_transfer_chain` 将 `final_quark_path` 传入 `_commit_downloading`；`_DownloadStateChanged` 分支在 `aria2.remove` 之外追加 `best-effort alist.remove(final_quark_path)`（失败仅告警）。

### D6 容量记账漏计窗口（P1-7）

**方案**：准入提交成功后对 `capacity.provider` 显式失效 used 缓存（新增 `invalidate_usage_cache()`），使下一轮准入立即反映刚落盘 downloading 文件；reserved 口径维持不变（downloading 不重复计）。

**替代方案**：reserved 纳入 downloading。否决——双计导致容量判定过严；缓存失效最小改动。

### D7 cancel/skip 同步 media 状态（P1-4）

**方案**：`queue.cancel_task` / `skip_task` 置 DQ 终态后，同一事务调用 `_sync_media_status(media_id, session)`（复用 transfer 既有 helper），media 无在途任务时回落 tracking。

### D8 P2 有界性修复

- **刮削连坐风暴**：`scrape_runner` 失败后对该 media 设置进程内退避（如 10min 内不再触发 force sync），并将「同步失败」与「节点重试计数」解耦：外部服务故障不批量累加全部 scrape 行 node_attempt。
- **下载重试必败**：`aria2.add_uri` 追加 `allow-overwrite=true` + `auto-file-renaming=false` options（需先确认 aria2 启动参数，见 Open Questions）。
- **集级确认 fail-open**：Emby 遗漏集列表为空时延迟一轮复核（不做立即 finalize）；电影分支入库确认前校验文件大小合理性。
- **通知节流**：`nastools_sync` 失败通知复用 `_alert_cooldown` 节流模式（进程内 + 落库 TTL 可选项）；`_alert_cooldown` 若多 worker 部署则迁移到 DB 节流（见 Open Questions 部署形态）。
- **quota_wait 写放大**：唤醒后先查容量余量，余量不足时跳过准入循环直接返回（减少 N×UPDATE + N×容量查询）。
- **凭据不完整**：取件时校验 file_name/file_size/share_code 齐全，缺失保持源行 ready + 告警，不建注定失败的 DQ。
- **`_node_failure` 清 save_task_id**：无条件清空改为 `WHERE status != 'transferring'` 条件化。
- **全量模式防重**：`_enqueue` 阶段对 `download_queue` 表补集号归一化防重（跨文件名同集）。
- **NaSTools token**：移除 `?token=` query 通道，仅保留 header（或文档明示风险后保留）。

## Risks / Trade-offs

- [D1 拆分事务后 CAS 窗口变化] → 事务 B 内锁行 + 重读 reserved 兜底，容量不超发；进程锁保准入段串行。
- [D2 哨兵误删正常任务] → 哨兵仅对「不在库 gid」操作，且连续 N 轮才触发；误删概率低，删除前告警。
- [D3 按文件名匹配失败率高] → 失败降级为「仅触发轮询」，由既有轮询路径推进，不丢任务。
- [D4 条件 INSERT 跨方言兼容] → SQLite 与 Postgres 均支持 `INSERT ... SELECT ... WHERE NOT EXISTS`，无方言分支。
- [D8 退避/节流均为进程内状态，重启即失] → 可观测性影响有限，重启后最多多触发一次重试；长期选项为落库节流。

## Migration Plan

- 纯后端修复，无 schema/API 变更；分 task 合并到既有 change 分支（current），按 tasks.md 顺序实施。
- 每项缺陷修复配套回归测试（backend/tests/），全量 `pytest` 通过后进入 verify。
- 回滚：修复按 task 独立提交，任一引入回归可单条 revert。

## Open Questions

- aria2 守护进程启动参数是否配置 `allow-overwrite`（影响 D8 下载重试项是否成立）——需在生产 aria2 上确认。
- 部署形态是否为单 worker（影响 D8 通知节流是否需落库）。
- cloudSaver `receiveCode=stoken` 契约在带提取码分享下是否成立（评审遗留待实测项，不属于本 change 修复范围，记录备用）。
