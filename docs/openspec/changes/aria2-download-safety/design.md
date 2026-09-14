## Context

现状约束（详见 proposal.md - Why）：

- `_admit_batch` 段 2（transfer.py）在每轮准入前调用 `aria2.tell_active` + `tell_waiting`，用 download_queue.aria2_gid 白名单校验陌生 gid；陌生 gid 触发 `_record_alert` 告警、跳过本轮转存，连续 3 轮（`_GID_STRIKE_LIMIT`）后 best-effort `aria2.remove` 强删。用户自行添加的下载任务因此被反复告警并可能被系统删除。
- 容量快照（quark_capacity_log）显示使用率峰值 40.2%（84.5/210 GB），系统容量告警（`check_capacity_alert`，阈值 90% + 连续 2 条快照）从未触发，通知表中无任何容量类 flow_error——「空间不足」提示的实际来源与触发路径待定位确认。
- `release_space_cleanup_job`（cleanup.py，每 12 小时）以 download_queue 非终态行的 quark_path/file_name 为引用集，其余 /quark 文件视为孤儿删除；用户自行下载（非系统 DQ 行）的源文件不在引用集内，可能被误删。

## Goals / Non-Goals

**Goals:**

- 消除陌生 gid 的拦截/告警/强删行为，用户自行下载任务与系统转存完全共存。
- 定位并消除「空间充足却提示空间不足」的来源，保证容量告警/提示与真实容量一致。
- 孤儿清理执行前保护 aria2 正在下载/等待下载的源文件，杜绝误删导致下载失败；aria2 不可用时清理 fail-safe（本轮不删）。

**Non-Goals:**

- 不引入新的「用户自行任务登记/管理」界面，不改动 download_queue 数据模型。
- 不改变容量准入（check/quota_wait）本身的预算公式，只修正告警/提示的口径与触发条件。
- 不做清理 UI 或手动清理入口；只保护既有自动清理路径（孤儿兜底清理），cancel/完成路径的删除有明确 DQ 引用语义，不在本次保护范围。
- 不实现 n8n 相关功能或兼容层。

## Decisions

### D1: 移除陌生 gid 拦截/告警/强删，转存不再依赖陌生 gid 判定

删除 `_admit_batch` 段 2 整段逻辑：不再调用 tell_active/tell_waiting 做来源校验、不再维护 `_unknown_gid_strikes`、不再产生「非本系统 aria2 任务」flow_error、不再执行 `aria2.remove(gid)`。

- 依据：n8n 已停用，「防双转存」场景消失；白名单口径本身无法区分「用户自行下载」与「误启动」，继续拦截只会误伤。
- 保留：阶段 A 对**本系统已签发 gid**（download_queue.aria2_gid 非空行）的 tellStatus 轮询、进度上报、完成推进与 recovery 回退清理，均不受影响（这些按 DQ 行驱动，不依赖陌生 gid 判定）。
- 替代方案对比：
  - 保留校验但改为「仅日志不告警不删除」：仍会每轮跳过转存（fail-closed 语义不变），转存继续被干扰；否决。
  - 配置开关控制校验启用：引入新配置面与状态，收益低；否决。彻底移除更符合「完全放行」决策。
- 连带：删除后 `_GID_STRIKE_LIMIT`、`_unknown_gid_strikes` 及相关测试断言一并清理。

### D2: 空间不足提示——先定位真实来源，再按口径修复

DB 证据（快照峰值 40.2%、通知表无容量类告警、`capacity_alert` task_run 均为「未触发告警」）表明：**系统容量告警路径从未实际发送过空间不足通知**。因此「空间不足」提示的可能来源需在实施中逐一排查并修正：

1. `check_capacity_alert`（capacity_alert.py）：确认触发门槛（阈值 90% + 连续 2 条 + 冷却 30min）与实际数据一致；快照来源 quark_capacity_log 的 total_gb/used_gb 正确（当前 210/84.5）。无缺陷则不改逻辑，仅复核。
2. `_try_admit_one` / `_admit_batch` 的 `_record_alert(category="capacity")`（「容量不足已累计 N 次」「容量不足已持续超过 24 小时」）：这些告警仅在真实容量 check 失败或 quota_wait 滞留超时才触发；复核 quota 读取（`_load_quota_gb` = system_config quark_quota_gb=210）与 `check()` 预算公式是否可能在小使用率下误判不足（如 margin 异常、Decimal 类型问题）。当前数据下不应触发；若存在误触发路径则修复。
3. 前端「等待容量」（QueueView.vue `quotaWaitText`）：仅当任务处于 quota_wait 状态才显示；若用户看到的是该文案，应确认 quota_wait 的产生与容量一致性（当使用率 40% 时不应出现 quota_wait）。
4. 历史对话/站内提示：若排查确认所有代码路径在 40% 使用率下均不会产生空间不足通知，则结论为「当前版本已与真实容量一致」，在验证阶段以快照+通知为空作为证据，无需改码。

- 实施顺序：先证据排查（脚本/查询），再决定修改点；spec 侧已固定行为契约（容量充足 MUST NOT 产生空间不足通知），设计上不预设具体缺陷。
- 替代方案：直接改 `capacity_alert_threshold` 或关停告警 job——治标不治本且削弱运维能力；否决。

### D3: 清理前保护 aria2 下载源文件（fail-safe）

`release_space_cleanup_job` 在孤儿判定（present − referenced）之后、`alist.remove` 之前插入下载源保护：

1. **扩展 aria2 查询字段**：`tell_active` / `tell_waiting` 的 keys 增加 `"files"`，从 `files[].uris[].uri` 提取下载源 URL；解析 URL path 的末段（URL-decoded basename）作为「正在下载/等待下载的源文件名」。aria2 1.36.0 实测 comment 会被丢弃，但 uris 字段在 tellStatus/tellActive 中可用（addUri 时传入）。
   - 说明：`files[].path` 是 aria2 本地落盘路径（下载目录），不是 /quark 源文件，**不用于**本保护；源文件只能从 uri 还原。
2. **保护集合并入孤儿判定**：`orphans = present − referenced − aria2_sources`；aria2 源文件名匹配采用与现有引用集相同的 basename 比较口径。
3. **fail-safe**：查询 aria2（tell_active + tell_waiting 任一失败，Aria2Unavailable）→ 本轮不删除任何孤儿，`record_task_run(cleanup, error, ...)` 并记录告警，下轮（12h 后）重试——在无法确认下载状态时绝不删除。
4. 封装：aria2 侧新增 `list_source_basenames()`（内部组合 tell_active/tell_waiting 并解析 uri），cleanup 侧调用并 try/except 兜底；保持 Aria2Client 单例注入便于测试 mock。

- 替代方案对比：
  - 只保护 DQ 非终态引用（现状）：覆盖不了用户自行下载任务；否决。
  - 清理前暂停 aria2 / 检查全局统计（numActive/numWaiting>0 就全量跳过）：过度保护，一个无关下载任务会冻结全部孤儿清理；否决。
  - 按目录整体保护（/quark 有活动任务则跳过整个清理）：粒度太粗，空间释放效率低；否决。
- 风险与边界：uri 可能带签名参数（query string）——只取 path 段并 basename，不受影响；个别驱动 uri 结构异常导致解析失败 → 该文件名不纳入保护集（仅 debug 日志），aria2 查询整体失败才 fail-safe（避免解析噪声放大为冻结）。

## Risks / Trade-offs

- [移除陌生 gid 拦截后，若未来重新启用 n8n 双转存会失去防线] → n8n 已停用且不在规划内；如未来复启用，应重新评估独立机制，不在本 change 内保留死代码。
- [uri 解析失败导致个别下载源未被保护] → 解析按「整体查询失败 fail-safe、单条解析失败降级跳过」分级；另 aria2 uris 为空的任务（极端）不纳入保护，风险接受并在测试覆盖常规场景。
- [空间不足来源排查可能最终「无码可改」，仅证据结论] → spec 已把行为契约（容量充足不提示）固定，验证阶段用真实快照 + 通知数据作为验收证据；若发现误触发路径则按 D2 修复。
- [扩展 tell_active/tell_waiting keys 增加响应体积] → 每轮/每次调用额外返回 files 字段，量级（每任务数个文件）可忽略；且 D1 移除后转存轮不再高频调用 tell_active/tell_waiting，仅清理 job（12h）使用。

## Migration Plan

- 纯后端改动，无数据迁移；部署后清理 job 自动获得保护行为。
- 回滚：D1 若回滚，恢复 `_admit_batch` 段 2 原逻辑即可（代码删除但 git 历史保留）；D3 回滚不影响既有孤儿清理。

## Open Questions

- 用户所见的「空间不足」站内提示的具体文案/时间戳暂未复现（通知表无容量类记录）；若实施排查仍无法复现，将按 D2 结论以「证据确认无误报路径 + 口径复核」交付，并在验证阶段向用户展示快照与通知数据。