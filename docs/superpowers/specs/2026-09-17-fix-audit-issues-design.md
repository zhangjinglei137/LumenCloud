---
comet_change: fix-audit-issues
role: technical-design
canonical_spec: openspec
---

# fix-audit-issues 深度技术设计

## Context

本 change 修复全量审查发现的 75 个问题（1 critical + 5 high + ~20 medium + ~49 low），跨 5 个功能域、6 大主题（安全/数据模型/队列容量契约/Emby-TMDB 一致性/资源性能/工程化 CI）。

技术栈约束：FastAPI + SQLAlchemy 2.0 async + APScheduler（PG 生产 / SQLite 测试双方言）、Vue3 + Pinia + Element Plus、Docker 非 root 部署、单 worker 设计。需求契约见 `docs/openspec/changes/fix-audit-issues/specs/` 12 个 delta spec；动机见 proposal.md；高层决策见 design.md（本文件为其深度细化，不替代）。

**已核实的关键事实**（决定本设计的具体方案）：
- 外键现状：`EpisodeState.media_id`(models:91)、`TaskQueue.media_id`(132)、`DownloadQueue.media_id`(174)、`task_queue_id`(176) 均 `ForeignKey` 无 `ondelete`；引用类 `WatchRequest.requested_by/reviewed_by`、`InviteCode.used_by` 亦是
- `TaskRun.media_id` 为 `Integer`(283)，`Media.id` 为 `BIG_PK`(BigInteger/sqlite Integer variant)
- 回调鉴权**已完备**：`notify.py:39-105` 已有 HMAC-SHA256（`X-Aria2-Signature` 头）+ 时间戳防重放（±15min）+ secret fail-closed(503)；`trigger_download_complete`(transfer.py:1718) 自带 `status=='downloading'` 条件门控与幂等 rowcount 校验
- Emby status 筛选：`_build_library_params`(emby.py:631-653) 将小写 `continuing/ended` 透传为 `SeriesStatus` 参数，未对齐 Emby 契约大小写
- 登录限流：`auth.py` 已有 `_login_rate_key` 进程内窗口限流（`_LOGIN_FAIL_MAX_KEYS=10000`），注册接口无对应保护
- 容量积压：`capacity.py:198-219 _pending_estimate_gb` 仍查旧 `TransferQueue.status=='pending'`；前端 `format.ts DOWNLOAD_ACTIVE_STATUSES` 漏 `'pending'`（后端 `queue.py _DQ_ACTIVE` 含）

## Goals / Non-Goals

**Goals:**
- 修复全部 75 个审查问题，每个 high/critical 修复有单测封闭
- 数据模型修复经 alembic 迁移落地，FG 与 SQLite 双方言兼容
- CI 自动运行 70+ 现有测试作为回归闸门
- 修复不破坏既有 API 契约（409 语义为新增、文档化）

**Non-Goals:**
- 不落地 token 双通道统一改造（仅修登出清 cookie 本体）
- 不引入分布式限流/分布式冷却（单 worker 现状，以注释+文档明示 trade-off）
- 不做多 worker 部署改造
- 不新增外部依赖

## Decisions

### D1. 数据模型：单一 alembic 迁移 `audit_fixes`

**迁移内容**（upsert 成对 upgrade/downgrade）：
- `ondelete=CASCADE`：`EpisodeState.media_id`、`TaskQueue.media_id`、`DownloadQueue.media_id`、`DownloadQueue.task_queue_id`、`DownloadTask.media_id`（若该表存在）
- `ondelete=SET NULL`：`WatchRequest.requested_by/reviewed_by`、`InviteCode.used_by`、`TaskRun.media_id`（引用类关系）
- `TaskRun.media_id` 类型对齐 `BIG_PK`
- 独立索引：`EpisodeState.media_id`、`TaskQueue.media_id`（`DownloadQueue.media_id` 已有 `idx_dqk_media`，核实后复用）

**理由**：DB 层最后防线，防应用层遗漏子表清理路径导致 IntegrityError。**备选**：仅改模型声明不迁移——生产存量表不生效，否决。

**风险/缓解**：
- [CASCADE 误删] → 仅对确认「随媒体删除而删除」的队列/状态表声明；引用类用 SET NULL；迁移前以 `test_delete_media_fk.py` 验证删除语义无回归
- SQLite 无 `ALTER COLUMN TYPE` → 类型对齐需表重建（`batch_alter_table`），downgrade 同步重建

### D2. 鉴权：token_version 改密吊销

- `users.token_version INTEGER NOT NULL DEFAULT 0`（迁移同 D1 或独立迁移）
- 签发：JWT payload 增 `"ver": user.token_version`
- 校验：`get_current_user`(deps.py) 解码后比对 `ver == user.token_version`，不符 → 401（视为令牌失效）
- 改密：`UPDATE users SET token_version = token_version + 1`（与密码更新同事务）

**理由**：无状态撤销 O(1)，无需 Redis/黑名单。**备选**：jti 黑名单（引入存储）、仅前端登出（无法驱逐已泄露 token）均否决。
**风险/缓解**：[改密即踢在线会话] → 预期语义，前端改密成功提示重新登录。

### D3. 注册限流：通用进程内限流器

- 抽取 `auth.py` 现有限流为 `app/services/rate_limit.py`：`RateLimiter(max_failures, window_seconds)`，进程内 dict（键=IP）+ 过期清理
- 注册接口：邀请码校验失败计数（422/400），窗口超限 → 429「尝试过于频繁」
- 登录限流迁移到同一实现（行为等价，不改变现有阈值语义）

**理由**：一致性 + 零依赖；邀请码 64bit 熵已防单点爆破。**风险/缓解**：[误伤合法新人] → 阈值宽松（与登录同级），429 附带重试提示。

### D4. 登出清 cookie

- 后端 `POST /api/auth/logout`（或改造现有登出）：`response.delete_cookie(settings.COOKIE_NAME, path=...)` 后返回成功；已有登出则补 delete_cookie 子句
- 前端 `stores/auth.ts logout`：先 await 登出接口，再清 localStorage token，最后跳登录页

**理由**：收敛「logout 后残留 httpOnly cookie 仍可鉴权」缺口。**风险/缓解**：[登出接口失败阻断本地登出] → 接口失败仅 warn，仍清 localStorage（不因后端故障锁死用户）。

### D5. CAS 修复统一模式

所有条件 UPDATE 遵循模板：

```python
stmt = update(T).where(T.id == x, T.status.in_(ALLOWED_STATES)).values(**vals)
r = await session.execute(stmt)
if r.rowcount == 0:  # 并发推进 → 冲突
    # 写路径（API 端点）→ raise HTTPException(409, "状态已变化，请刷新后重试")
    # 内部路径（调度/任务）→ return/continue 并标记 conflict，不误报其他语义
```

应用点：
- `queue.py sort_task`(840-846)：up/down 交换加 `status=='pending'` 门控
- `queue.py add_queue_task`(748-754)：重置加 `status IN (pending,error,unmatched,ready)`，probing 拒绝
- `queue.py retry_task`(629-634)：旧三表分支检查 rowcount → 409
- `transfer.py _try_admit_one`(1316-1332)：quota_wait 分支 rowcount==0 → set conflict（与抢占分支一致）
- `cancel_task`(316-382)：对 downloading 行先 `aria2.tell_status` 确认非 complete；complete → 走 `_complete_download` 路径；remove 失败不静默吞完成态

**理由**：同一类并发缺陷统一修复模式，减少心智负担。**风险/缓解**：[409 语义前端未处理] → 前端冲突提示复用 422 错误处理路径（拦截器统一 toast）。

### D6. 容量口径与前端活跃状态对齐

- `capacity.py _pending_estimate_gb`：改 `select(func.coalesce(func.sum(DownloadQueue.file_size), 0)).where(DownloadQueue.status == "pending")`，与 `_reserved_gb` 口径一致，区分准入门禁（pending 不占容量，仅预估展示）
- `format.ts DOWNLOAD_ACTIVE_STATUSES` 补 `'pending'`
- 前后端测试各补一条口径断言

### D7. 有界缓存统一模式

统一实现工具 `app/utils.py`（或各自文件内）：

```python
class BoundedLRUCache:
    def __init__(self, max_items): self._d: OrderedDict = ...
    def get(self, k): ...  # 命中 move_to_end
    def set(self, k, v): self._d[k]=v; self._d.move_to_end(k); while len>max: popitem(last=False)
```

应用点：
- `tmdb.py _SEASON_AIR_CACHE`/`_ALL_EPS_CACHE`（上限如 5000）
- `emby.py _INGESTED_CACHE`/`_recent_empty_check`（上限如 2000）
- `poster.py _POSTER_CACHE`：满 100 淘汰最旧再写入；回源响应校验 `content-type` 前缀 `image/`，非图片不缓存返 502
- 顺手：`refresh_episode_info`(tmdb.py:584-607) 批量 upsert（`bulk_insert_mappings` 或 PG `INSERT..ON CONFLICT`，SQLite 分支兼容 upsert）

**理由**：长跑进程内存收敛，海报缓存不再「满 100 实质失效」。**风险/缓解**：[LRU 淘汰热门] → OrderedDict 语义即最近访问保留，命中率与规模匹配。

### D8. CI：新增 `.github/workflows/ci.yml`

三个 job：
1. `backend-test`：setup-python 3.12 → `pip install -r backend/requirements.txt` → `pytest backend/tests`
2. `frontend-test`：setup-node 22 → `npm ci`(frontend) → `npm test` + `npm run build`
3. `docker-build`：`docker build` 冒烟（`.trivyignore` 沿用既有忽略，trivy 为可选 job）

本地先逐一跑通等价命令再合入 workflow（E1 审查已确认 70+ 测试存在但无 CI）。**风险/缓解**：[CI 环境依赖差异] → 本地先 `pytest`/`npm test`/`npm build` 各绿；workflow 失败即拦截合并。

### D9. scan.py PG 到期过滤表达式修正

现状：`bindparam("now") - (minutes * text("interval '1 minute'"))`（scan.py:1535-1538）Column×TextClause 语义不确定。

修正（PG 分支）：
```python
minutes = func.coalesce(...)
due_filter = ...  # 方案：
# A: bindparam 传分钟数，PG 用 (now - make_interval(mins => :minutes))，SQLite 保持现有
# B: 若按每影视各自 interval → literal_column("interval '1 minute'") * minutes（已核实 SQLAlchemy 2.0 支持 Column*literal 生成 interval 乘法）
```

先写双方言 SQL 渲染单测（`test_scan_due_filter.py`：直接断言 `str(expr.compile(dialect=postgresql))` 与 sqlite 均合法且不抛），再改实现。若 A/B 在实库验证有问题，回退为 Python 侧取任务再过滤（不变量：到期判定语义不因方言变化）。

### D10. 首启 job 默认 paused

`main.py` lifespan（或 `scheduler._apply_job_switches`）：`system_config` 表为空（首启）时，所有 job 开关返回 false（保持 paused）；管理员设置页显式开启。已有开关键存量数据的行为不变。

**理由**：首启无凭据时 scan/transfer 空转刷屏外部服务。**风险/缓解**：[已部署升级后空表误判] → 仅当表完全为空（非空表即使无 job 键也走默认值）时生效。

### D11（新增）. 队列/容量前端契约与通知/日志/海报专项

- `QueueView`「仅看活跃」补 pending（D6 前端部分）；计时器按 activeTab watch 启停（切 Tab 停 progressTimer）
- 站内通知分页（`notifications.py` 加 limit/offset/total 返回，前端铃铛用首页切片）+ 清理任务补 notifications 保留期删除（`cleanup.py prune_history_job` 增加一段，沿用 retention 配置）
- PushPlus 失败补发站内 `flow_error` 通知（`PushPlusNotifier.notify` except 分支调 InAppNotifier）
- 通知文案脱敏：`notify_templates.flow_error_nastools_sync(exc)` 截断 + `urlsplit` 剥 userinfo/query token
- `run-logs` 任务类型筛选改后端下发（`logs.py` 增加 `task_types` 枚举返回，前端 LogsView 动态渲染）
- 邀请码上限前后端一致（后端 `le=50`，前端 `:max="50"` 或常量共享）

**理由**：列为独立决策以覆盖 open 阶段 tasks.md 中未落入 D1-D10 的中等修复，保证实现阶段不遗漏。

## Risks / Trade-offs

- [外键 CASCADE 误删] → D1 风险缓解；迁移前测试验证
- [token_version 踢在线] → 预期；前端提示
- [注册限流误伤] → 宽松阈值 + 429 文案
- [CI 首次接入红] → 本地等价命令先行；workflow 失败拦截
- [单 worker 冷却重复告警] → 注释 + 文档明示；不引入分布式状态
- [scan SQL 双方言差异] → 渲染单测 + 必要时回退 Python 过滤
- [单大 change 验收面广] → tasks.md 按组勾选 + CI 总闸

## Migration Plan

1. 迁移 batch A：D1 alembic → 空跑验证 → 副本演练 → 生产
2. batch B：D2/D3/D4 鉴权与凭据（在线兼容：改密前旧 token 仍有效；遮蔽前端配套）
3. batch C：D5/D6/D11 队列/容量/契约（无迁移，纯代码+测试）
4. batch D：D7/D8/D9/D10 缓存/CI/SQL/首启
5. 回滚：git revert 逐批；alembic downgrade 成对

## 测试策略

- **高优修复单测**：注册限流 429、token_version 改密吊销、CAS 冲突 409、pending 口径、CASCADE 删除语义、scan SQL 双渲染、回调鉴权补测（现状满足 + 测试封闭）
- **回归**：CI 跑全量（backend pytest 70+ 文件、frontend vitest、npm build）
- **新增覆盖**：QueueView 控制面操作交互、http 401/redirect 守卫、notifications 分页清理、poster 缓存淘汰与类型校验
- **验证命令**：`pytest backend/tests`、`cd frontend && npm test && npm run build`、`docker build`

## Open Questions

无阻塞项。build 阶段现场验证（不改变本设计决策）：
- A6 Emby `SeriesStatus` 大小写实际契约（`.capitalize()` 保守修复 + 实测确认）
- `DownloadTask` 表是否含 media FK（若有补进 D1 迁移）
- 通知分页沿用 logs offset 模式（已定，确认接口形状即可）