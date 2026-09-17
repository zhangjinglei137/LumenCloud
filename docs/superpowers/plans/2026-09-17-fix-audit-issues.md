---
change: fix-audit-issues
design-doc: docs/superpowers/specs/2026-09-17-fix-audit-issues-design.md
base-ref: 114b8081c182581a02a49435cfd4f317b58ac2f5
---

# fix-audit-issues 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次性修复全量审查发现的 75 个问题（1 critical + 5 high + ~20 medium + ~49 low），覆盖安全、数据模型、队列容量契约、Emby/TMDB 一致性、资源性能与工程化 CI 六大主题，每个 high/critical 修复有单测封闭，CI 自动运行 70+ 现有测试。

**Architecture:** 按 Design Doc 决策 D1-D11 分四个实现批次（A 数据模型迁移 → B 鉴权与凭据 → C 队列/容量/契约 → D 缓存/CI/SQL/首启/杂项），每批独立提交与测试闭环，最终由 CI 作为总闸。修复遵循统一模式：外键级联走单一 alembic 迁移；鉴权用 token_version 无状态吊销；并发修复统一「状态门控 + rowcount 校验」CAS 模式；缓存统一 OrderedDict LRU 有界化。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + alembic + APScheduler（PG 生产/SQLite 测试双方言）；Vue3 + Pinia + Element Plus + vitest；GitHub Actions CI。

**Spec:** `docs/openspec/changes/fix-audit-issues/`（proposal/design/tasks + 12 个 delta spec）——计划从 spec 论证，执行者须同时阅读 Design Doc `docs/superpowers/specs/2026-09-17-fix-audit-issues-design.md`。

## Global Constraints

- 产物语言 zh-CN（注释/commit message 中文，commit 遵循 Conventional Commits）
- 数据库双方言兼容：PG（生产）与 SQLite（测试），数据模型改动必须双方言可迁移、测试可建表
- 不新增外部依赖；不改变既有 API 契约（409 语义为前提允许的新增，须文档化）
- 不主动启动/停止/重启任何服务；验证依赖测试命令而非运行中的服务
- 每次提交前必须跑相关测试；每批结束跑全量 `pytest backend/tests` + `cd frontend && npm test`
- 单 worker 部署假设保持（多 worker 相关修复以注释+文档明示 trade-off，不引入分布式状态）

---

## 批次 A：数据模型与迁移（Design D1）

### Task A1: alembic 迁移 audit_fixes 骨架 + 外键级联

**Files:**
- Create: `backend/alembic/versions/xxxx_audit_fixes.py`
- Modify: `backend/app/models/__init__.py`（各 FK 声明加 ondelete）

**Interfaces:**
- Consumes: 现有 `BIG_PK`、`Base`、各模型类
- Produces: 迁移 `upgrade()`（级联 ALTER + 类型对齐 + 索引）与 `downgrade()` 成对可逆

- [x] **Step 1** 在 `models/__init__.py` 中为子表 FK 补 `ondelete`：
  - `EpisodeState.media_id`(L91)、`TaskQueue.media_id`(L132)、`DownloadQueue.media_id`(L174)、`DownloadQueue.task_queue_id`(L176) → `ForeignKey("media.id", ondelete="CASCADE")`、`ForeignKey("task_queue.id", ondelete="CASCADE")`（核实 DownloadTask 表含 media FK 则一并）
  - `WatchRequest.requested_by/reviewed_by`、`InviteCode.used_by`、`TaskRun.media_id`（若声明 FK）→ `ondelete="SET NULL"`
- [x] **Step 2** 编写 alembic 迁移（PG 用 `batch_alter_table`/直接 ALTER；SQLite 类型对齐需 `batch_alter_table` 表重建）：CASCADE/SET NULL 外键约束重建 + `TaskRun.media_id` 类型对齐 `BigInteger` + `EpisodeState.media_id`/`TaskQueue.media_id` 独立索引
- [x] **Step 3** 验证迁移：`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`（或等效迁移命令）空跑通过，双向可逆
- [x] **Step 4** 运行 `test_delete_media_fk.py` 及 `test_api_smoke.py`，验证 CASCADE 改动后删除媒体语义无回归
- [x] **Step 5** Commit：`feat: 数据模型外键级联与类型对齐迁移`

### Task A2: 前端类型契约对齐

**Files:**
- Modify: `frontend/src/types/index.ts:50`（`MediaItem.tmdb_id`）

**Interfaces:**
- Consumes: `MediaItem` 接口定义
- Produces: `tmdb_id: number | null`（与后端 Media.tmdb_id nullable 对齐）

- [x] **Step 1** 将 `MediaItem.tmdb_id` 改为 `number | null`
- [x] **Step 2** 验证 `cd frontend && npx vue-tsc --noEmit`（或 `npm run build` 的类型检查）通过
- [x] **Step 3** Commit：`fix: 前端 MediaItem.tmdb_id 类型对齐后端 nullable 契约`

---

## 批次 B：鉴权与凭据安全（Design D2/D3/D4）

### Task B1: token_version 改密吊销

**Files:**
- Modify: `backend/app/models/__init__.py`（User 表加 `token_version`）
- Modify: `backend/app/routers/auth.py`（改密端点递增）、`backend/app/routers/deps.py`（get_current_user 校验 ver）
- Test: 新增 `backend/tests/test_token_version.py`（或并入 test_auth 系列）

**Interfaces:**
- Consumes: `User` 模型、`get_current_user`、JWT 签发函数
- Produces: JWT payload 新增 `ver` 键；User.token_version 递增逻辑

- [ ] **Step 1** User 表加 `token_version = mapped_column(Integer, server_default=text("0"), nullable=False)`（含迁移）
- [ ] **Step 2** 写失败测试：旧 token 在改密后请求受保护端点应 401
- [ ] **Step 3** 运行确认失败
- [ ] **Step 4** 签发时 payload 加 `ver`；`get_current_user` 解码后与 `user.token_version` 比对，不符 → 401；改密事务内 `token_version += 1`
- [ ] **Step 5** 运行测试确认通过；跑 test_auth 全系列确认无回归
- [ ] **Step 6** Commit：`feat: 修改密码吊销既有令牌（token_version）`

### Task B2: 注册邀请码限流 + 登录限流复用

**Files:**
- Create: `backend/app/services/rate_limit.py`
- Modify: `backend/app/routers/auth.py`（注册端点计限流、登录改复用）
- Test: 新增 `backend/tests/test_register_rate_limit.py`

**Interfaces:**
- Consumes: 现有 `_login_rate_key` 语义
- Produces: `RateLimiter` 类（`check(key)`/`hit(key)`），注册 429 行为

- [ ] **Step 1** 抽 `rate_limit.py`：进程内 dict + 窗口计数 + 过期清理，接口 `check(key)->bool`、`hit(key)`、`reset(key)`
- [ ] **Step 2** 写失败测试：窗口内连续无效邀请码 → 429；正常注册不受影响
- [ ] **Step 3** 运行确认失败
- [ ] **Step 4** 注册端点邀请码校验失败路径 `hit`，超限 `raise 429`；登录限流迁移到新实现（阈值语义不变）
- [ ] **Step 5** 运行测试确认通过；test_auth 无回归
- [ ] **Step 6** Commit：`feat: 注册接口邀请码爆破限流`

### Task B3: 登录时序侧信道抹平

**Files:**
- Modify: `backend/app/routers/auth.py`（登录逻辑）
- Test: 新增测试（用户不存在时对 dummy hash 执行校验）

**Interfaces:**
- Consumes: bcrypt `verify_password`
- Produces: 无（行为等价，时序一致）

- [ ] **Step 1** 写失败测试：不存在用户路径调用 `verify_password(dummy_pw_digest, input)` 至少一次（mock 断言）
- [ ] **Step 2** 实现：用户不存在时对固定 dummy bcrypt hash 执行一次校验再返回统一 401
- [ ] **Step 3** 运行测试通过；test_auth 无回归
- [ ] **Step 4** Commit：`fix: 抹平用户名枚举时序侧信道`

### Task B4: 登出清 cookie

**Files:**
- Modify: `backend/app/routers/auth.py`（logout 端点 delete_cookie）
- Modify: `frontend/src/stores/auth.ts`（logout 先调后端再清 localStorage）

**Interfaces:**
- Consumes: 现有 logout 端点、httpOnly cookie 名称（settings）
- Produces: `POST /api/auth/logout` 成功时响应 `delete_cookie`

- [ ] **Step 1** 写失败测试：登出后携带残留 cookie 请求受保护端点 → 401
- [ ] **Step 2** logout 端点响应 `delete_cookie(settings cookie 名, path=...)`
- [ ] **Step 3** 前端 `auth.ts logout`：await 登出接口（失败仅 warn）→ 清 localStorage → 跳登录
- [ ] **Step 4** 运行测试通过；前端 build 通过
- [ ] **Step 5** Commit：`fix: 登出清除 httpOnly cookie 会话`

### Task B5: 登录 redirect 白名单

**Files:**
- Modify: `frontend/src/views/LoginView.vue`（redirect 校验）
- Test: 新增 `frontend/src/views/LoginView.test.ts`（或并入现有）

**Interfaces:**
- Consumes: `useRoute`/`useRouter`
- Produces: 无

- [ ] **Step 1** 写失败测试：`redirect=https://evil.com` 回退 `/`；`redirect=/media/1` 保留
- [ ] **Step 2** 实现 `safeRedirect()`：`^/` 开头且非 `//` 则用，否则 `/`
- [ ] **Step 3** 前端测试通过 + build 通过
- [ ] **Step 4** Commit：`fix: 登录 redirect 白名单校验防钓鱼跳转`

### Task B6: 并发审批 IntegrityError → 409

**Files:**
- Modify: `backend/app/routers/approvals.py`（approve 并发兜底）
- Test: 新增 `backend/tests/test_approval_dup.py` 补充并发场景

**Interfaces:**
- Consumes: `Media.tmdb_id` UNIQUE 约束、现有审批 CAS
- Produces: 冲突返回 409「该影视已在影视库」

- [ ] **Step 1** 写失败测试：两并发 approve 同 tmdb_id，后者收到 409 而非 500
- [ ] **Step 2** 实现：`try/except IntegrityError` → 409（事务回滚后 wr 状态回 pending）
- [ ] **Step 3** 运行测试通过；test_approval_dup 无回归
- [ ] **Step 4** Commit：`fix: 并发审批同 tmdb_id 返回 409 而非 500`

### Task B7: admin 初始密码落盘 + webhook 密钥遮蔽

**Files:**
- Modify: `backend/app/routers/auth.py`（初始密码生成与输出）、`backend/app/routers/settings.py`（敏感键清单）、`frontend/src/views/SettingsView.vue` + `frontend/src/config/settingsMeta.ts`（凭据表单）
- Test: 新增 `backend/tests/test_init_admin_password.py`（可选并入）、settings 相关测试

**Interfaces:**
- Consumes: `_SENSITIVE_KEYS`、`_WHITELIST_EXACT`、`_EDITABLE_KEYS`、config_store
- Produces: 初始密码写入 `data/` 600 权限文件；`internal_aria2_webhook_secret`/`internal_nastools_webhook_token` 纳入敏感键

- [ ] **Step 1** 写失败/前置测试：GET /api/settings 不再明文返回 webhook 密钥（`***`）；启动初始密码不再出现在日志
- [ ] **Step 2** 初始密码生成改写入 `data/.initial_admin_credential` 600 权限文件（日志仅提示文件路径）
- [ ] **Step 3** settings.py 将两个 webhook 键加入 `_SENSITIVE_KEYS`
- [ ] **Step 4** 前端凭据表单：两键以密码框/「已配置」占位，留空 = 不修改（`dirtyCredKeys` 机制复用）
- [ ] **Step 5** 运行测试 + 前端 build 通过
- [ ] **Step 6** Commit：`fix: admin 初始密码落盘与 webhook 密钥遮蔽`

---

## 批次 C：队列/容量/契约（Design D5/D6/D11）

### Task C1: 容量积压预估口径修正

**Files:**
- Modify: `backend/app/routers/capacity.py:198-219`（`_pending_estimate_gb`）
- Test: 新增 `backend/tests/test_capacity_pending_estimate.py`

**Interfaces:**
- Consumes: `DownloadQueue` 模型、`_reserved_gb` 口径
- Produces: `pending_estimate` 基于 DownloadQueue.pending 行 SUM(file_size)

- [ ] **Step 1** 写失败测试：DownloadQueue 有 pending 行时容量接口返回非空积压预估
- [ ] **Step 2** 改 `_pending_estimate_gb` 查 `DownloadQueue`（`status=="pending"` + SUM(file_size)），注释更新
- [ ] **Step 3** 运行测试通过 + test_capacity 无回归
- [ ] **Step 4** Commit：`fix: 容量积压预估改查 DownloadQueue 修复失真`

### Task C2: 前端仅看活跃补 pending

**Files:**
- Modify: `frontend/src/utils/format.ts:342-348`（`DOWNLOAD_ACTIVE_STATUSES`）
- Test: 更新 `frontend/src/views/QueueView.test.ts` 或 format.test

**Interfaces:**
- Consumes: 后端 `_DQ_ACTIVE`（含 pending）
- Produces: `DOWNLOAD_ACTIVE_STATUSES` 含 `'pending'`

- [ ] **Step 1** 写失败测试：仅看活跃时 pending 行被展示
- [ ] **Step 2** `DOWNLOAD_ACTIVE_STATUSES` 补 `'pending'`
- [ ] **Step 3** 前端测试 + build 通过
- [ ] **Step 4** Commit：`fix: 仅看活跃包含排队中任务（pending）`

### Task C3: 队列控制面 CAS 门控（sort/add/retry）

**Files:**
- Modify: `backend/app/routers/queue.py`（sort_task L840-846、add_queue_task L748-754、retry_task L629-634）
- Test: 新增 `backend/tests/test_queue_cas_conflict.py`

**Interfaces:**
- Consumes: `DownloadQueue`/`TaskQueue` 状态字段
- Produces: 冲突时 409「状态已变化，请刷新后重试」；probing 行不被重置

- [ ] **Step 1** 写失败测试三条路径：在途排序被拒、probing 重置被拒、retry 状态变化返回 409
- [ ] **Step 2** 实现统一 CAS 门控（WHERE 状态集 + rowcount==0 → 409）
- [ ] **Step 3** 运行测试通过 + test_transfer/test_queue 无回归
- [ ] **Step 4** Commit：`fix: 队列控制面操作补 CAS 状态门控`

### Task C4: 准入与取消状态一致性（transfer）

**Files:**
- Modify: `backend/app/tasks/transfer.py`（`_try_admit_one` quota_wait 分支 L1316-1332、`cancel_task` 相关）
- Modify: `backend/app/routers/queue.py`（cancel_task L316-382）
- Test: 新增 `backend/tests/test_transfer_quota_conflict.py`、`test_cancel_completed.py`

**Interfaces:**
- Consumes: `aria2.tell_status`、`_INFLIGHT_STATUSES`
- Produces: quota_wait 分支 CAS 未命中 → conflict 跳过（不误报）；cancel 已 complete → 走完成路径

- [ ] **Step 1** 写失败测试：准入并发推进后不误报 quota_wait；取消 aria2 已 complete 任务不标 failed
- [ ] **Step 2** 实现：quota_wait 分支 rowcount==0 设 conflict；cancel 先 tell_status 判 complete 分路径
- [ ] **Step 3** 运行测试通过 + test_transfer 全系列无回归
- [ ] **Step 4** Commit：`fix: 准入冲突识别与取消已完成任务状态一致性`

### Task C5: 回调鉴权补测试封闭（B8 现状满足）

**Files:**
- Test: 新增 `backend/tests/test_notify_auth_closed.py`（或并入 test_nastools_notify 风格）

**Interfaces:**
- Consumes: `notify.py` 现有 HMAC 校验、`trigger_download_complete`
- Produces: 鉴权行为回归测试（无代码改动预期，除非测试暴露缺口）

- [ ] **Step 1** 写测试：无签名/错误签名/超时戳 → 401/503；合法签名 + 非 downloading gid → False 不推进
- [ ] **Step 2** 运行测试；如暴露缺口（如 gid 未限定 media 维度）则按 D11 补，否则仅测试
- [ ] **Step 3** Commit：`test: 下载完成回调鉴权与幂等封闭`

### Task C6: aria2 故障不触发回退循环

**Files:**
- Modify: `backend/app/tasks/recovery.py`（超时回退前 aria2 探活）或 `transfer.py` 轮询
- Test: 新增 `backend/tests/test_recovery_aria2_unavailable.py`

**Interfaces:**
- Consumes: `aria2` client、`get_global_stat`/`tell_status`
- Produces: aria2 故障时 downloading 任务不被回退

- [ ] **Step 1** 写失败测试：aria2 抛 Aria2Unavailable 时 downloading 任务不触发回退
- [ ] **Step 2** 实现：recovery 判定下载中任务超时前先探活 aria2（失败则跳过本轮）
- [ ] **Step 3** 运行测试通过 + test_recovery 无回归
- [ ] **Step 4** Commit：`fix: aria2 故障期间不触发下载任务回退循环`

### Task C7: 站内通知分页与清理

**Files:**
- Modify: `backend/app/routers/notifications.py`（limit/offset/total）
- Modify: `backend/app/tasks/cleanup.py`（`prune_history_job` 补 notifications 清理）
- Modify: `frontend/src/stores/notifications.ts`（分页参数）
- Test: 新增 `backend/tests/test_notifications_pagination_cleanup.py`

**Interfaces:**
- Consumes: `Notification` 模型、`retention_days` 配置
- Produces: `GET /api/notifications?limit=&offset=` + total；保留期外通知删除

- [ ] **Step 1** 写失败测试：通知分页返回与总条数；清理任务删除超期通知
- [ ] **Step 2** 实现通知接口分页；cleanup 补 notifications 保留期删除
- [ ] **Step 3** 前端 store 适配分页参数；运行测试 + build
- [ ] **Step 4** Commit：`feat: 站内通知分页与定期清理`

### Task C8: PushPlus 失败降级 + 文案脱敏

**Files:**
- Modify: `backend/app/services/notifier.py`（PushPlusNotifier 失败补站内告警）
- Modify: `backend/app/services/notify_templates.py`（异常截断脱敏）
- Test: 新增测试（推送失败产生站内通知；异常文案脱敏）

**Interfaces:**
- Consumes: `InAppNotifier`、`flow_error` 事件、`urlsplit`
- Produces: 经配置开关控制的降级站内通知；脱敏后文案

- [ ] **Step 1** 写失败测试：PushPlus.send 抛错 → 产生站内 flow_error 通知；异常消息含 URL token → 通知正文已脱敏
- [ ] **Step 2** 实现：except 分支调 InAppNotifier 补发（防递归：降级通知本身不重推 PushPlus）；`flow_error_nastools_sync` 截断 + 剥 userinfo/query
- [ ] **Step 3** 运行测试通过；notifier 相关无回归
- [ ] **Step 4** Commit：`feat: PushPlus 失败降级站内告警并脱敏通知文案`

### Task C9: 海报缓存淘汰 + content-type 校验

**Files:**
- Modify: `backend/app/services/poster.py`（`_POSTER_CACHE` LRU、content-type 校验）
- Test: 新增 `backend/tests/test_poster_cache_type.py`

**Interfaces:**
- Consumes: `_POSTER_CACHE_MAX`、httpx 响应
- Produces: 满时淘汰最旧；非 `image/*` 不缓存返 502/降级

- [ ] **Step 1** 写失败测试：缓存满后新海报仍可缓存；上游返回 text/html → 不缓存且非 200 图片返回
- [ ] **Step 2** 实现 LRU（OrderedDict）+ content-type 前缀校验
- [ ] **Step 3** 运行测试通过 + test_poster 无回归
- [ ] **Step 4** Commit：`fix: 海报缓存满淘汰与响应类型校验`

### Task C10: 运行日志任务类型后端下发 + 前端常量同步

**Files:**
- Modify: `backend/app/routers/logs.py`（返回 task_types 枚举或独立端点）
- Modify: `frontend/src/views/LogsView.vue`（动态选项）、`frontend/src/utils/format.ts`
- Test: 后端 logs 测试补充断言

**Interfaces:**
- Consumes: `record_task_run` 实际 task_type 集合
- Produces: 筛选选项由后端下发

- [ ] **Step 1** 写失败测试：logs 响应含当前任务类型枚举
- [ ] **Step 2** 实现后端下发（复用 logs 页现有过滤字段）；前端动态渲染
- [ ] **Step 3** 运行测试 + build 通过
- [ ] **Step 4** Commit：`feat: 运行日志任务类型筛选项后端下发`

### Task C11: 邀请码上限与其余 small 契约

**Files:**
- Modify: `backend/app/routers/admin.py:28`（le=50 保持）、`frontend/src/views/UsersView.vue:210`（:max 同步 50）
- Modify: `frontend/src/views/MediaDetailView.vue:35`（NaN 守卫）、`frontend/src/views/QueueView.vue`（计时器 watch）、`frontend/src/components/TmdbSearch.vue`（错误态）

**Interfaces:**
- Consumes: 各视图现有行为
- Produces: 上限一致、NaN 防御、计时器启停、搜索错误反馈

- [ ] **Step 1** 邀请码 `:max="50"` 与后端对齐（前端测试更新）
- [ ] **Step 2** MediaDetailView 加 `Number.isFinite(mediaId)` 守卫（非法 → 回列表页）
- [ ] **Step 3** QueueView 计时器按 activeTab watch 启停
- [ ] **Step 4** TmdbSearch 加 catch 错误态标记
- [ ] **Step 5** 前端测试 + build 通过
- [ ] **Step 6** Commit：`fix: 邀请码上限对齐与前端防御性修复`

---

## 批次 D：缓存/CI/SQL/首启/杂项（Design D7/D8/D9/D10/D11）

### Task D1: 有界缓存统一（tmdb/emby）

**Files:**
- Modify: `backend/app/services/tmdb.py`（`_SEASON_AIR_CACHE`/`_ALL_EPS_CACHE`、`refresh_episode_info` N+1）
- Modify: `backend/app/services/emby.py`（`_INGESTED_CACHE`/`_recent_empty_check`、server_id/user_id 配置指纹、`list_all_library` 冗余精简、status capitalize）
- Test: 新增 `backend/tests/test_cache_bounded.py`（含 LRU 淘汰 + 配置切换缓存失效）

**Interfaces:**
- Consumes: `OrderedDict` LRU 工具、`_config_fingerprint()`
- Produces: 有界缓存 + 配置变更失效 + Emby status 大小写对齐

- [ ] **Step 1** 写失败测试：缓存超上限淘汰最旧；修改 Emby 配置后 server_id 重新获取；SeriesStatus 用 `Continuing/Ended` 传入
- [ ] **Step 2** 实现 `BoundedLRUCache`（utils）并接入 tmdb/emby 各缓存；`_SERVER_ID`/`_USER_ID` key 附加 `_config_fingerprint()`；`_build_library_params` 对 status `.capitalize()`
- [ ] **Step 3** `refresh_episode_info` 批量 upsert；`list_all_library` 拍平两段式赋值
- [ ] **Step 4** 运行测试通过；test_emby_*/test_tmdb_* 无回归
- [ ] **Step 5** Commit：`perf: 进程内缓存有界化与 Emby 配置失效联动`

### Task D2: scan.py PG 到期过滤修正 + _enqueue 提交语义

**Files:**
- Modify: `backend/app/tasks/scan.py`（`_due_filter` L1535-1538、`_enqueue` L1303-1365）
- Test: 新增 `backend/tests/test_scan_due_filter.py`（双方言渲染单测）

**Interfaces:**
- Consumes: `bindparam`/`func.make_interval`/`literal_column`
- Produces: PG/SQLite 双方言合法到期过滤；入队上下文统一管理

- [ ] **Step 1** 写失败测试：渲染 `_due_filter` 在 PG 与 SQLite 方言下编译不抛、语义等价
- [ ] **Step 2** 实现 PG 分支 `func.make_interval(mins=...)` 或 `literal_column("interval '1 minute'") * minutes` 稳妥写法；`_enqueue` 移除显式 commit，冲突捕获单条跳过
- [ ] **Step 3** 运行渲染单测通过；test_scan_* 无回归
- [ ] **Step 4** Commit：`fix: 巡检到期过滤 SQL 双方言正确性与入队提交语义`

### Task D3: NasTools 会话重登 + 同步锁粒度

**Files:**
- Modify: `backend/app/services/nastools.py`（`_do` 401/403 清 cookie 重登一次）
- Modify: `backend/app/tasks/nastools_sync.py`（锁缩小临界区，sleep 移出）
- Test: 新增 `backend/tests/test_nastools_relogin.py`

**Interfaces:**
- Consumes: `_ensure_login`、`_session_cookie`
- Produces: 会话失效自动重登；冷却 sleep 不持锁

- [ ] **Step 1** 写失败测试：`_do` 收到 401 后自动重登成功；重登仍败上抛
- [ ] **Step 2** 实现 `_do` 重登（限一次重试）；`_sync_lock` 只保护冷却检查+时间戳更新
- [ ] **Step 3** 运行测试通过 + test_nastools 无回归
- [ ] **Step 4** Commit：`fix: NasTools 会话失效自动重登与同步锁粒度`

### Task D4: CI workflow + 工程化杂项

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `backend/app/main.py`（日志挂载 lifespan 化、首启 job 策略、serve_spa /internal 404）、`backend/app/scheduler.py`（首启空表 paused）、`backend/app/json.py`（版本守卫）
- Modify: `backend/app/database.py`（PG 连接池显式配置）

**Interfaces:**
- Consumes: 现有 lifespan、`_apply_job_switches`、`system_config`
- Produces: 三 job CI；首启空表不激活 job

- [ ] **Step 1** 本地跑通 `pytest backend/tests`、`cd frontend && npm test && npm run build`
- [ ] **Step 2** 写 `.github/workflows/ci.yml`（backend-test / frontend-test / docker-build 三 job）
- [ ] **Step 3** 实现 main.py 日志显式 handler + lifespan 首启空表 job 开关 paused + serve_spa internal 404；database.py 连接池 `pool_size/max_overflow/pool_recycle`；json.py 版本守卫
- [ ] **Step 4** 运行相关测试（test_api_smoke/test_scheduler）+ build
- [ ] **Step 5** Commit：`feat: 新增 CI workflow并加固启动/回退路径`

### Task D5: 剩余 low 项收尾

**Files:**
- Modify: `backend/tests/test_scheduler.py:68`（注释 10 个 job）
- Modify: `frontend/src/api/http.ts`（toLogin 评估记录）等按需

**Interfaces:**
- Consumes: 各文件现状
- Produces: 注释/文档一致性

- [ ] **Step 1** 修正 test_scheduler 过时注释与其余文档化 low 项
- [ ] **Step 2** 运行全量测试确认无回归
- [ ] **Step 3** Commit：`chore: 修正过时注释与 low 级文档一致性`

---

## 批次 E：全量验证与收尾

### Task E1: 全量回归

**Files:** 无新文件（验证任务）

- [ ] **Step 1** 运行 `pytest backend/tests` 全绿
- [ ] **Step 2** 运行 `cd frontend && npm test && npm run build` 全绿
- [ ] **Step 3** 对照审查清单逐一核对 75 个问题修复状态（每批 commit 消息可追溯）
- [ ] **Step 4** 记录构建证据：`comet state record-check fix-audit-issues build --command "<实际命令>" --exit-code 0`
- [ ] **Step 5** Commit（如有收尾改动）：`chore: 全量检查修复收尾`

## Self-Review 对照

- **Spec 覆盖**：12 个 delta spec 的 requirement 均有对应 Task（auth-session→B1/B2/B3/B4/B5；emby-library-browse→D1；media-detail-ui→C11；media-pipeline→D2/D3；notifications→C7/C8；pipeline-admission→C1/C4；pipeline-transfer→C3/C4/C5/C6；poster-proxy→C9；queue-inspection-display→C2；run-logs→C10；settings-credentials-ui→B7/C11；user-invite-management→C11/B2）
- **tasks.md 覆盖**：40 项任务全部映射（1.x→D4、2.x→A1/A2、3.x→B1-B7、4.x→B7/C11、5.x→C1-C6、6.x→D1、7.x→C7-C10、8.x→D2/D3/D4、9.x→C11、10.x→E1）
- **类型一致**：`token_version`、`BoundedLRUCache`、`RateLimiter`、`ver` payload 在各任务间签名一致
- **Placeholder 检查**：无 TBD/TODO；每任务含具体文件、行号、验证命令