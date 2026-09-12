## 1. P0-1 准入事务边界重构（transfer.py + capacity.py）

- [x] 1.1 重构 `_try_admit_one` 准入段：拆为「短事务 A 读 pending 快照 → 锁外容量 check → 短事务 B（锁行 + 重读 reserved + CAS 抢占/置 quota_wait）」，并验证 `backend/tests/` 新增事务边界回归测试通过（断言容量 check 期间无活动 DB 事务、CAS 条件更新仍生效）
- [x] 1.2 将容量快照落库（`_persist_snapshot`）调用移出任何外层 DB 事务上下文，并验证新增测试确认无嵌套 session 提交

## 2. P1-2 GID 白名单逃生通道（transfer.py + recovery.py）

- [x] 2.1 放宽 GID 白名单口径为「DB 中 aria2_gid 非空全部行」（不再限定 downloading），并验证现有 GID 校验测试更新后通过
- [x] 2.2 增加陌生 gid 连续跳过哨兵：进程内计数 ≥3 轮执行一次 `aria2.remove(gid)` best-effort + 告警，并验证新增测试覆盖「孤儿 gid 不再永久阻断转存」

## 3. P1-1/P1-3 webhook 文件级推进与节点重置（nastools_notify.py）

- [x] 3.1 `_advance_scrape_to_library` 改为从载荷提取文件名/集号定位单行推进；无法定位时仅触发 library_check 轮询加速，不再批量推进，并验证新增 webhook 推进测试通过
- [x] 3.2 推进 UPDATE 补齐 `node_attempt=0 / node_finished_at=now / node_error=None`，并验证断言节点字段重置的测试通过

## 4. P1-4 cancel/skip 同步 media 状态（routers/queue.py）

- [x] 4.1 `cancel_task` / `skip_task` 置 DQ 终态后同一事务调用 `_sync_media_status`，并验证新增测试覆盖「取消最后一个任务后 media 状态回落 tracking」

## 5. P1-5 转存提交冲突清理夸克残留（transfer.py）

- [x] 5.1 `_transfer_chain` 将 `final_quark_path` 传入 `_commit_downloading`，`_DownloadStateChanged` 分支追加 best-effort `alist.remove(final_quark_path)`，并验证新增测试覆盖冲突分支清理调用

## 6. P1-6 取件-创建原子化（transfer.py）

- [x] 6.1 `_fetch_from_task_queue` 改为单语句条件 INSERT（`INSERT ... SELECT ... WHERE NOT EXISTS`）实现取件与 DQ 创建原子一致，并验证新增测试覆盖「撞 UNIQUE 时源行不误标 done」

## 7. P1-7 容量记账漏计窗口（capacity.py + transfer.py）

- [x] 7.1 `CapacityProvider` 新增 `invalidate_usage_cache()`，准入提交成功后调用失效 used 缓存，并验证新增测试覆盖「刚落盘 downloading 文件计入下一轮准入」

## 8. P2 有界性修复

- [x] 8.1 刮削连坐风暴：`scrape_runner` 失败后对 media 设置进程内退避（10min），同步失败与节点重试计数解耦，并验证新增测试覆盖「NasTools 故障不批量累加全部 scrape 行 node_attempt」
- [x] 8.2 `aria2.add_uri` 追加 `allow-overwrite=true` + `auto-file-renaming=false` options（先确认生产 aria2 启动参数，若已配置则跳过并记录），并验证 aria2 RPC shape 测试更新通过
- [x] 8.3 集级确认 fail-open：Emby 遗漏集为空时延迟一轮复核；电影入库确认前校验文件大小合理性，并验证对应测试更新
- [x] 8.4 nastools_sync 失败通知接入节流（复用 `_alert_cooldown` 模式），并验证节流后不重复通知的测试通过
- [x] 8.5 quota_wait 唤醒后先查容量余量，余量不足直接返回（减少写放大与容量查询），并验证测试覆盖「积压时不进入准入循环」
- [x] 8.6 取件凭据完整性校验：file_name/file_size/share_code 缺失保持源行 ready + 告警，不建注定失败的 DQ，并验证测试覆盖
- [x] 8.7 `_node_failure` 无条件清 save_task_id 改为 `WHERE status != 'transferring'` 条件化，并验证测试覆盖「不抹掉并发方新 save 的 task_id」
- [ ] 8.8 全量模式防重：`_enqueue` 对 download_queue 补集号归一化防重（跨文件名同集），并验证测试覆盖「同集不同命名不再重复入队」
- [ ] 8.9 NaSTools webhook token 移除 `?token=` query 通道（或经确认保留并补文档说明），并验证鉴权测试更新

## 9. 集成验证

- [ ] 9.1 全量运行 `backend/tests/` pytest 通过（含既有 + 新增回归测试），确认无破坏主链路状态机与 CAS 幂等协议
- [ ] 9.2 复查 design.md Open Questions 处置记录（aria2 参数确认结果、部署 worker 数），更新至文档或另立后续 change
