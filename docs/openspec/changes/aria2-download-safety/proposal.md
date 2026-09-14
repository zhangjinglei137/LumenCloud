# Proposal: aria2-download-safety

## Why

1. **陌生 gid 告警误报且误删用户任务**：转存准入前的 GID 来源校验会把用户自行添加到 aria2 的活动/等待任务判为「非本系统任务」（n8n 误启动假设），连续 3 轮后执行 `aria2.remove` 强制删除——用户自行下载的源任务被系统直接杀除，且每分钟轮询产生大量无效告警（单日 130 条 flow_error）。
2. **空间不足提示与真实容量矛盾**：网盘实际使用率约 40%（84.5/210 GB，alist 检测），但用户多次收到「网盘空间不足」类系统通知/站内提醒，造成恐慌与无效运维动作。
3. **清理兜底可能误删下载源文件**：网站自动孤儿清理（每 12 小时）只保护下载队列引用的文件，用户自行下载（非系统发起）的 aria2 任务其源文件不在保护集内，可能被当孤儿删除导致下载失败。

现在 n8n 已停用，不存在「n8n 误启动双转存」风险，来源校验的拦截语义已失去依据。

## What Changes

- **GID 来源校验从「拦截 + 告警 + 删除」改为「完全放行」**：aria2 活动/等待任务中的陌生 gid（非本系统 download_queue 已签发）不再导致转存本轮跳过、不再产生 flow_error 告警、不再触发 best-effort `aria2.remove`。转存流程移除对陌生 gid 的依赖，仅保留本系统自有 gid 的日常跟踪。
- **定位并修复「空间不足」误报来源**：排查容量/空间相关通知与提示（`_record_alert(category="capacity")`、`check_capacity_alert`、前端「等待容量」文案）的产生条件与数据依据，确保在容量充足（使用率低于告警阈值 90%）时不产生空间不足类系统通知，且在已确认无误报的前提下无法复现时修正提示口径与数据来源说明。
- **清理保护下载中/等待下载的源文件（新能力）**：夸克孤儿清理在执行删除前查询 aria2 活动（active）+ 等待（waiting）任务的下载源文件集合（经 files/uri 归一为 /quark 路径文件名），与候选孤儿集合求差后再删除；正在下载或排队等待下载的文件一律跳过，从源头避免删除下载源导致 aria2 下载失败。aria2 状态查询失败时按 fail-safe 处理（本次不删除任何孤儿，仅告警记录），不得误删。

## Capabilities

### New Capabilities

- `quark-cleanup-safety`: 夸克清理（孤儿文件兜底清理等删除性操作）执行前保护 aria2 正在下载/等待下载的源文件，避免误删导致下载失败。

### Modified Capabilities

- `pipeline-admission`: 「转存来源校验与逃生」要求变更——陌生 aria2 gid（非系统已签发）不再拦截转存、不再告警、不再自动清理删除；校验的逃生语义由「防 n8n 双转存」改为与用户自行下载共存。
- `notifications`: 空间不足类通知（容量告警/等待容量提示）的触发须与实际容量数据一致——容量充足（使用率低于阈值）时不得产生「空间不足」类系统通知或站内提醒，避免与真实容量矛盾的误报。

## Impact

- `backend/app/tasks/transfer.py`：`_admit_batch` 段 2 GID 来源校验逻辑（删除陌生 gid 拦截/告警/删除分支及 `_unknown_gid_strikes` 计数）、容量不足告警文案与触发条件复核。
- `backend/app/tasks/cleanup.py`：`release_space_cleanup_job` 增加 aria2 下载源保护（query active+waiting → 归一文件名 → 求差再删）。
- `backend/app/services/aria2.py`：`tell_active`/`tell_waiting` 扩展返回下载源字段（`files[].uris[].uri` 或 `files[].path`），供清理保护解析文件名。
- `backend/app/services/capacity.py` / `backend/app/tasks/capacity_alert.py`：容量告警触发条件与文案复核（确认与真实容量一致）。
- `backend/app/tasks/transfer.py`：`_record_alert(category="capacity")` 触发条件复核。
- 前端 `frontend/src/views/QueueView.vue`：「等待容量」文案与数据来源复核（仅在有真实容量不足证据时展示）。
- 测试：`backend/tests/test_transfer.py`、`backend/tests/test_capacity_alert.py`、`backend/tests/test_capacity.py`、新增清理保护测试。