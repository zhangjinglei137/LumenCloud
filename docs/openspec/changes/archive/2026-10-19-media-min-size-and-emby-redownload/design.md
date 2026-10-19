---
comet_change: media-min-size-and-emby-redownload
role: technical-design
canonical_spec: openspec
archived-with: 2026-10-19-media-min-size-and-emby-redownload
status: final
---

## Context

现状与约束（动机见 proposal.md - Why；验收以 specs/ 下 delta spec 为准）：

- 巡检入队的大小过滤位于 `backend/app/tasks/scan.py`：`_size_limits()` 按「media 覆盖 > system_config 全局 > `settings` 默认」解析单集/电影上限，`_size_limit_gb()` 选出生效值，主循环与纯数字兜底分支分别判断 `file_size > limit_gb * 1024**3` 后跳过；未知大小走 fail-closed 跳过。配置面由 `routers/settings.py` 白名单、`routers/media.py` 的 `MediaPatch`/DTO、前端 `settingsMeta.ts` / `MediaDetailView.vue` 组成。
- 防重权威源是 `download_queue`（`UNIQUE(media_id, episode)`）。`_enqueue()` 对同 `media_id` 下同 `episode` 或同 `file_name`、以及同集归一化引用命中的 `download_queue` 记录（**任意状态**）都返回 `existing`；`task_queue` 同键/同文件名同样拦截。
- 巡检 `_resolve_done_states()` 只查询 `status='done'` 的 `download_queue`：Emby 已确认入库 → 删除该行解除防重；Emby 仍缺失 → `done→failed`（`retry_count+1`），达到 `_DONE_FAIL_RETRY_LIMIT=3` 才写 `failed` 终态。第一次转换后状态已不是 `done`，该行此后再也不会被巡检选中——`failed` 记录永久阻挡重新入队，只能人工 `retry_task`。
- 关键事实（决定宽限取值）：`library_check` 的入库轮询只在 **Emby 已确认收录**时才置 `done`（`_finalize_done`，同时删除夸克文件）。故 `done` 记录事后在 Emby 缺失，基本可判定为 Emby 侧被删除，而非入库延迟。
- 转存取件（`transfer._promote`）只在「同键 `download_queue` 不存在」时从 `task_queue` 建行；`retry_task` 已定义 failed → pending 的重置语义（清 `retry_count/node_attempt/save_task_id/save_attempt_at/error/node_*`）。

## Goals / Non-Goals

**Goals:**

- 大小过滤从「仅上限」扩展为「上下限」，配置形态与现有上限完全同构（全局 + 单影视覆盖 + 前端可编辑），默认值可关闭。
- 让「用户已在 Emby 删除」的集按已确认的宽限轮次（3 轮）自动回到可下载状态，并以自动重下次数上限（3 次）兜底，避免无限循环。

**Non-Goals:**

- 不改造转存/下载/刮削/入库状态机，不引入新队列或新调度。
- 不改变容量准入与 reserved 记账口径。
- 不改变「未知大小 fail-closed 跳过」策略。
- 不为大小下限新增独立告警/通知；仍复用巡检结果的 `size_filtered` 计数与日志。
- 不新增分享失效检测：重下沿用原分享快照，失效按现有转存失败路径处理。

## Decisions

### D1. 上下限统一为一对边界值，过滤点在既有大小判断处

- `_size_limits()` 返回 `(ep_min, ep_max, movie_min, movie_max)`，`_size_limit_gb()` 改为 `_size_bounds_gb()` 返回 `(min_gb, max_gb)`；`None`/`0` 表示该维度不限制。
- 主循环与纯数字兜底分支共用同一判定：`min_bytes <= file_size <= max_bytes` 才放行（边界值放行）；越界计入 `size_filtered`，日志区分「低于下限」与「高于上限」。
- 备选：在 `_walk_share` 阶段提前过滤——否决，`_walk_share` 还要为全量模式收集文件，提前过滤会让统计口径分散，且现有过滤点已在匹配之后、入队之前，语义最清晰。

### D2. 配置面完全照抄现有上限的三层结构

- 新增全局键 `min_episode_size_gb` / `min_movie_size_gb`（默认 0.1 / 0.5 GB，写入 `settings` 常量与 `settings.py` 白名单）；`Media` 新增 `min_episode_size_gb` / `min_movie_size_gb` 可空列，经 `MediaPatch` 与 DTO 透出。
- 前端沿用元数据驱动：`settingsMeta.ts` 增两条配置说明（含「留空或 0 表示不限制」），`MediaDetailView.vue` 在现有两个 `el-input-number` 旁各加一个最小值输入（`:min="0"`，空值 = 不限制）。
- 备选：单一全局阈值不分单集/电影——否决，与现有上限口径不一致，电影与单集的合理区间差异明显。

### D3. 防重解除状态机扩展：done 与「Emby 未入库 failed」统一处理

- `_resolve_done_states()` 的查询范围从 `status='done'` 扩展为：
  - `status='done'`，或
  - `status='failed'` 且 `node_error IN (_DONE_FAIL_ERROR, _DONE_LIMIT_ERROR)`（只接管本规则产生的失败，不碰其它失败类型）。
- 对仍缺失的集：
  - `retry_count < _DONE_FAIL_RETRY_LIMIT`（=3，**用户确认的宽限轮次**）→ 保持/转为 `failed` 并 `retry_count+1`；
  - `retry_count >= 宽限上限` 且 `auto_redownload_count < _AUTO_REDOWNLOAD_LIMIT`（=3）→ 按 `retry_task` 的重置语义置为 `pending`（`retry_count=0`、`node_attempt=0`、清 `save_task_id/save_attempt_at/error/node_error/node_*`），并 `auto_redownload_count+1`；
  - 否则 → 写 `failed` 终态 + 上限文案（保留人工重试入口）。
- 对 Emby 已确认入库的集：删除该行（对 `done` 与上述 `failed` 行都适用），解除防重；顺带修掉「本规则产生的失败行永不被清理」的泄漏。
- 备选：直接删除记录让下一轮重新探测——否决，删除会丢失自动重下计数与已收集的转存凭据，且需要额外新建 `task_queue` 行才能重新探测，改动面更大、UNIQUE 冲突更多。
- 备选：宽限 0 轮（确认即重下）——用户选择 3 轮：`done` 虽已过一次 Emby 确认，但仍保留宽限以吸收偶发接口抖动。

### D4. 自动重下复用已有 `download_queue` 行，不新建任务队列行

- 重置为 `pending` 后由现有下载队列消费直接取件：`download_queue` 自带 `share_code/fids/fid_tokens/folder_id` 快照，无需重新探测；`_enqueue()` 因同键记录存在而继续返回 `existing`，天然避免重复入队。
- `auto_redownload_count` 为新列（`download_queue`，Integer，`server_default 0`，非空）；人工 `retry_task` 时同步清零，视为人工重新给足自动重下预算。

### D5. 宽限轮次沿用 `retry_count`，不新增计时字段

- `retry_count` 已在本规则内表达「done→failed 循环次数」，扩展后语义仍是「本集因 Emby 未确认而消耗的宽限轮次」（上限 3）；达到上限即触发自动重下，不引入时间窗口字段，避免与 per-media 巡检间隔配置耦合。
- 并发安全：所有更新/删除保持 `WHERE status=<读取时状态>` 条件更新，CAS 未命中即跳过该条，不中断巡检。

## Risks / Trade-offs

- [Emby 侧抖动导致正常下载被误判缺失并重下] → 宽限 3 轮（默认巡检间隔 60 分钟，约 3 小时），且自动重下最多 3 次；超过后转终态不再自动重下。
- [旧分享快照在重下时已失效] → 转存失败走现有失败/重试路径，最终转终态并可由人工处理；本 change 不增加失效检测。
- [大小下限误杀低码率短片或短剧] → 默认值 0.1GB 仅拦截明显异常的极小文件；全局或单影视留空/0 即关闭，单影视可覆盖。
- [新增列迁移] → 采用既有「带 `server_default` 的 ADD COLUMN」模式（SQLite/PostgreSQL 兼容），与 0016/0018 同型；`downgrade` 对称删列。
- [`failed` 行被自动重置可能与人工操作竞争] → 条件更新（CAS）保护；人工 `retry_task` 同样走条件更新并清零自动重下计数。
- [本轮巡检因 `_resolve_done_states` 异常中断] → 调用方已有 try/except 兜底，仅告警不阻断巡检。

## Migration Plan

1. 新增 alembic 迁移 `0019`（`down_revision = "0018_user_token_version"`）：`media` 加 `min_episode_size_gb` / `min_movie_size_gb`（可空），`download_queue` 加 `auto_redownload_count`（非空，默认 0）。
2. 部署时执行 `alembic upgrade head`；存量行自动获得默认值，行为变化仅为新增下限（默认值生效）与更积极的防重解除。
3. 回滚：`alembic downgrade -1` 删除新增列；旧代码不读取新列，可安全回退。
