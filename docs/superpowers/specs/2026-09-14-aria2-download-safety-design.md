---
comet_change: aria2-download-safety
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-14-aria2-download-safety
status: final
---

# aria2-download-safety 技术设计

## Context

本 change 围绕影视下载链路的三项关联问题（动机详见 proposal.md）：

1. **陌生 gid 误报与误删**：`_admit_batch`（transfer.py）段 2 在每轮准入前用 download_queue.aria2_gid 白名单校验 aria2 活动/等待任务；不在白名单的 gid（用户自行添加的下载）被反复判为「非本系统任务」告警，连续 3 轮（`_GID_STRIKE_LIMIT`）后 best-effort `aria2.remove(gid)` 强删。n8n 已停用，该防线失去依据。
2. **空间不足提示矛盾**：quark_capacity_log 快照峰值 40.2%（84.5/210 GB），`check_capacity_alert`（阈值 90% + 连续 2 条 + 冷却 30min）从未触发，通知表无容量类 flow_error。用户仍收到「空间不足」提示，来源待定位。
3. **孤儿清理误删下载源**：`release_space_cleanup_job`（cleanup.py，每 12h）以 download_queue 非终态行引用为保护集，用户自行下载任务（无 DQ 行）的 /quark 源文件不在保护集，可能被当孤儿删除导致 aria2 下载失败。

关键现状（代码级核实）：
- `tell_active`/`tell_waiting`（aria2.py）当前 keys 不含 `files`，且唯一调用方就是 `_admit_batch` 段 2。
- `_record_alert`（transfer.py:807）写 task_run(error) + flow_error 通知，`category="capacity"` 分支仅在真实容量 check 失败 / quota_wait 滞留超 24h 时触发。
- `check_capacity_alert`（capacity.py:327）评估 quark_capacity_log 最近 2 条快照使用率 ≥ 阈值才告警。
- 前端「等待容量」文案（QueueView.vue `quotaWaitText`）仅当任务 status=quota_wait 时展示。

## Goals / Non-Goals

**Goals:**

- 转存准入不再因陌生 gid 跳过/告警/删除，用户自行下载任务与系统转存共存。
- 空间不足提示与真实容量一致；若存在误报路径则修复，否则以数据验收。
- 孤儿清理删除前保护 aria2 正在下载/等待下载的源文件；aria2 不可用时清理 fail-safe（本轮不删）。

**Non-Goals:**

- 不引入用户自行任务登记/管理界面，不改 download_queue 数据模型。
- 不改容量准入（check/quota_wait）预算公式，只修告警/提示口径。
- 不做清理 UI；只保护既有自动孤儿清理路径（cancel/完成路径删除有明确 DQ 引用语义，不在保护范围）。
- 不实现任何 n8n 兼容/恢复逻辑。

## Decisions

### D1: 移除陌生 gid 拦截/告警/强删

删除 `_admit_batch` 段 2 整段逻辑（transfer.py:1641-1692 附近）：

- 删除调用：`aria2.client.tell_active()` / `tell_waiting()` 的来源校验调用。
- 删除状态：`_unknown_gid_strikes` dict、`_GID_STRIKE_LIMIT = 3` 常量。
- 删除动作：陌生 gid 的 strikes 计数、flow_error 告警（`_record_alert(category="gid")`）、best-effort `aria2.remove(gid)` 强删、跳过本轮转存。
- 连带：`_admit_batch` 的 1b「无 pending 空跑」检查仍保留（空跑判断与 gid 无关）。

保留不动：
- 阶段 A `_poll_downloading_tasks` 对本系统已签发 gid 的 tellStatus 轮询、进度刷新、complete/error 推进、recovery 超时回退——全部按 DQ 行（aria2_gid 非空）驱动，不依赖陌生 gid 判定。
- `queue.py` `_cleanup_cancel_side_effects` 对自有 gid 的 aria2.remove（用户主动取消语义，保留）。

理由：白名单口径无法区分「用户自行下载」与「误启动」，n8n 已停，拦截只会误伤；彻底移除优于配置开关（开关引入配置面且 fail-closed 语义仍会干扰转存）。

测试影响：test_transfer.py / test_capacity.py 中针对陌生 gid 拦截、告警、强删的用例改为「放行共存」语义（陌生 gid 存在时转存正常继续、无告警、无 remove 调用）。

### D2: 空间不足提示——证据排查 + 口径修复

实施步骤（以证据为准，不预设缺陷）：

1. **核对容量告警门槛**：`check_capacity_alert` 的 threshold（system_config `capacity_alert_threshold`=0.9）、连续条数（2）、冷却（30min）与 quark_capacity_log 数据（峰值 40.2%）——确认无任何情况下误触发。产出：核对结论。
2. **核对 `_record_alert(category="capacity")` 触发点**：
   - `_try_admit_one`「容量不足已累计 N 次」（`_QUOTA_REJECT_ALERT_THRESHOLD`）
   - `_admit_batch`「容量不足已持续超过 24 小时」（`_QUOTA_WAIT_ALERT_HOURS`）
   - 两者都只在真实容量 check 失败 / quota_wait 滞留超时触发；当前数据下不应触发。若 40% 使用率下出现 quota_wait（疑似误判），复核 `CapacityProvider.check()` 预算公式与 `_load_quota_gb` 读取（system_config=210）。
3. **核对前端「等待容量」文案**：`quotaWaitText` 仅在 quota_wait 展示；`quota_hint` 数据来源与容量一致性复核。
4. **证据交付**：若全部路径确认无误报，则以「快照峰值 40.2% + 通知表无容量类记录 + capacity_alert 全部 skipped」作为验收证据，不改码；向用户展示证据并确认其看到的具体文案来源（可能为前端「等待容量」或历史/对话提示）。

spec 契约（已固定）：容量充足 MUST NOT 产生空间不足类系统通知或站内提醒。

测试影响：test_capacity_alert.py / test_capacity.py 补充/确认「使用率低于阈值不告警」「无 quota_wait 时前端不展示等待容量」用例。

### D3: 清理保护 aria2 下载源（fail-safe）

**aria2.py 变更**：

1. `tell_active()` keys 增加 `"files"`：`[["gid", "status", "comment", "totalLength", "completedLength", "files"]]`。
2. `tell_waiting()` keys 同样增加 `"files"`。
3. 新增 `list_source_basenames() -> set[str]`：
   - 组合 `tell_active()` + `tell_waiting()`（异常向上抛 Aria2Unavailable）。
   - 遍历每任务 `files[].uris[].uri`（URI 列表），`urllib.parse.urlsplit(uri).path` 取 path，再 `unquote` 取末段 basename。
   - 解析失败/空值仅 debug 日志跳过（单条降级，不放大为整体失败）。
   - 说明：`files[].path` 是 aria2 本地落盘路径（download_dir + out），**不使用**；源文件只能从 uris 还原。

**cleanup.py 变更**（`release_space_cleanup_job`）：

1. 孤儿判定（present − referenced）之后、删除之前调用 `aria2.client.list_source_basenames()` 获取下载源保护集。
2. `orphans = sorted(present - referenced - aria2_sources)`。
3. 查询 aria2 异常（Aria2Unavailable 及其它）→ **fail-safe**：本轮不删除任何孤儿，`record_task_run(cleanup, error, "aria2 状态查询失败，本轮跳过清理: ...")` + `_record_alert` 类告警（沿用既有告警机制），return。
4. 其余路径不变（无孤儿 skipped、删除成功 success）。

理由与替代方案：
- 只保护 DQ 非终态引用（现状）：覆盖不了用户自行下载任务 → 否决。
- 清理前检查全局统计（numActive/numWaiting>0 全量跳过）：一个无关下载冻结全部清理 → 否决。
- 按目录整体保护：粒度太粗、释放效率低 → 否决。
- 按 files/uris 精确匹配 basename：粒度准、实现直接、可测试 → 采用。

测试影响：新增 cleanup 保护测试（mock aria2 + mock alist）：
- active 任务下载源文件被保护（不进删除列表）；
- waiting 任务源文件被保护；
- 无 aria2 任务 / 源不匹配 → 正常清理；
- aria2 查询异常 → alist.remove 不被调用、task_run(error) 记录。

## Risks / Trade-offs

- [移除 gid 校验后 n8n 复启用将无防线] → n8n 已停用且不在规划；复启用时另行设计，不在本 change 保留死代码。
- [uri 解析失败导致个别下载源未被保护] → 分级容错：整体查询失败 fail-safe（不删）、单条解析失败仅降级（debug）；uris 空任务不纳入保护，风险接受，测试覆盖常规场景。
- [D2 排查可能「无码可改」仅证据结论] → spec 契约固定行为，验证阶段以真实快照 + 通知数据验收；发现误触发路径则修复。
- [tell_active/tell_waiting 扩展 files 增加响应体积] → D1 后转存轮不再高频调用，仅清理 job（12h）使用，量级可忽略。
- [quota_wait 若在 40% 使用率下出现将指向容量判断缺陷] → D2 步骤 2 专门复核 `CapacityProvider.check()` 与 quota 读取，属 D2 范围，不扩大为独立改动。

## Migration Plan

- 纯后端改动，无数据迁移、无配置变更；部署后清理 job 自动获得保护行为。
- 回滚：git revert；D1 恢复段 2 原逻辑，D3 移除保护集求差即可，均不影响既有转存/清理主路径。

## Open Questions

- 用户所见的「空间不足」站内提示的具体文案/时间戳暂未复现（通知表无容量类记录）。若实施排查仍无法复现，按 D2 步骤 4 以证据确认交付，并向用户展示快照与通知数据确认其看到的具体来源。
