# Brainstorm Summary

- Change: aria2-download-safety
- Date: 2026-09-14

## 确认的技术方案

### D1: 移除陌生 gid 拦截/告警/强删（pipeline-admission）
- 删除 `_admit_batch` 段 2 整段：tell_active/tell_waiting 来源校验、`_unknown_gid_strikes` 计数、flow_error 告警、best-effort `aria2.remove(gid)` 强删。
- 清理 `_GID_STRIKE_LIMIT` 常量与相关测试断言。
- 保留阶段 A 对本系统已签发 gid（download_queue.aria2_gid 非空行）的 tellStatus 轮询/进度/完成推进/recovery 清理。

### D2: 空间不足提示来源排查与口径修复（notifications）
- 证据：quark_capacity_log 峰值 40.2%（84.5/210 GB），check_capacity_alert 从未触发（task_run 全部「未触发告警」），通知表无容量类 flow_error，`_record_alert(category="capacity")` 仅在真实容量 check 失败或 quota_wait 滞留超 24h 时触发。
- 实施：逐一核对 4 个来源（check_capacity_alert 门槛、_record_alert capacity 触发点、前端 quotaWaitText 文案、quota_wait 产生路径）。若发现容量充足时误触发路径 → 修复触发条件；若确认无误报路径 → 以快照+通知数据为验收证据，不改码。
- 用户反馈来源为「系统通知/站内提醒」，但通知表无容量类告警——需在实施中向用户展示证据并确认其看到的具体文案（可能是前端「等待容量」或旧数据）。

### D3: 清理保护 aria2 下载源（quark-cleanup-safety）
- 扩展 `Aria2Client.tell_active`/`tell_waiting` 返回 keys 增加 `"files"`；新增 `list_source_basenames()`：组合 tell_active+tell_waiting，从 `files[].uris[].uri` 解析 URL path 末段（URL-decoded basename）作为下载源文件名集合。
- `release_space_cleanup_job` 孤儿判定后（present − referenced）再求差 aria2 源保护集：orphans = present − referenced − aria2_sources。
- aria2 查询失败（Aria2Unavailable）→ fail-safe：本轮不删任何孤儿、record_task_run(cleanup, error) + 告警。
- 边界：files[].path 是本地落盘路径（非 /quark 源），只用 uris 解析源文件名；单条 uri 解析失败仅 debug 降级，整体查询失败才 fail-safe。

## 关键取舍与风险

- 移除 gid 校验后无 n8n 双转存防线 → n8n 已停用，未来复启用时重新评估。
- uri 解析失败个别源未被保护 → 分级容错（整体失败 fail-safe / 单条失败降级）。
- D2 可能最终「无码可改」仅证据结论 → spec 契约固定（容量充足 MUST NOT 提示），以数据验收。
- tell_active/tell_waiting 扩展 files 增加响应体积 → D1 后转存轮不再高频调用，仅清理 job（12h）使用，可忽略。

## 测试策略

- 后端全量 pytest（backend/tests/），重点：test_transfer.py（gid 校验用例改放行）、test_capacity_alert.py/test_capacity.py（容量一致）、新增 cleanup 保护测试（active 保护/waiting 保护/无任务正常清理/fail-safe）。
- 测试清单与三个 delta spec 场景逐条对应（tasks.md 4.2）。

## Spec Patch

无（open 阶段三个 delta spec 已覆盖全部验收场景，brainstorming 未发现需要回写的缺口）。
