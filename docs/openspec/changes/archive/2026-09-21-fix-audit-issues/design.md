## Context

本 change 是「全量检查后修复」：对 backend（FastAPI，routers/services/tasks/models）与 frontend（Vue3 + Pinia）的 5 个功能域审查发现 75 个问题，一次性修复。动机见 proposal.md。需求契约见 specs/ 下 12 个 delta spec（1 新能力 auth-session + 11 修改能力）。

现有约束：
- 数据层 SQLAlchemy 2.0 async + alembic 迁移，PG（生产）与 SQLite（测试）双方言
- 调度基于 APScheduler，job 冷切换（paused 起步，DB 开关控制）
- 鉴权现状：JWT（HS256，密钥文件 chmod 600）+ 后端签发 httpOnly cookie + 前端 localStorage Bearer 双通道并存
- 配置入库：system_config 表 + config_store 热加载，敏感键 GET `***` 遮蔽（现遮蔽 jwt_secret/admin 密码，webhook 密钥未遮蔽）
- 部署：单 worker（config_store 已注明单 worker 设计），Docker 非 root

## Goals / Non-Goals

**Goals:**
- 修复全部 75 个审查问题，且修复可验证：新增测试封闭每个 high/critical 修复，CI 让 70+ 现有测试自动运行
- 数据模型修复（外键级联/类型/索引）通过 alembic 迁移落地，PG 与 SQLite 双方言兼容
- 鉴权增强（注册限流/token 吊销/登出清 cookie）对现有合法用户流不破坏（消失的会话需重新登录属预期）
- 修复所有修复点保持「保存即生效」与现有 API 契约（除明确文档化的 409 语义新增外）

**Non-Goals:**
- 不引入新功能、不重构架构（如 token 双通道统一为纯 cookie 属后续会话改造，不在本 change 落地——但登出清 cookie 本体修复即做）
- 不裁剪现有 spec 契约；只按 delta 增改行为
- 不做多 worker 部署改造（容量告警冷却问题以「DB 落冷却或文档化 trade-off」二选一，倾向文档化 + 注释明确，因当前单 worker 部署）
- 不迁移 n8n 遗留、不改外部服务（Emby/Aria2/NasTools）既有契约

## Decisions

### D1. 数据模型修复走单一 alembic 迁移

外键 `ondelete`（EpisodeState/TransferQueue/DownloadQueue/DownloadTask/TaskQueue 等子表 CASCADE、引用类 SET NULL）、`TaskRun.media_id` 类型对齐 BigInteger、`media_id` 独立索引，合并为**一个**新 alembic revision（升级 + 降级成对），命名 `audit_fixes`。

- **备选**：不迁移、仅改模型声明 → 生产存量表不生效，放弃。
- **理由**：PG 需真实 DDL；SQLite 测试库由模型建表自动覆盖；单迁移便于验收与回滚。

### D2. 鉴权采用 token_version 实现改密吊销

users 表新增 `token_version int default 0`；JWT payload 带 `ver`，`get_current_user` 校验 `ver == user.token_version`；改密时 `token_version += 1`。旧 token 立即失效，无需黑名单存储。

- **备选 A**：JWT 黑名单（Redis/DB 存 jti）→ 引入状态存储，超出当前规模。
- **备选 B**：仅强制前端 logout → 无法驱逐已泄露 token，否决。
- **理由**：token_version 实现轻量、校验 O(1)、无新增依赖，语义等价「改密即作废」。

### D3. 注册限流复用登录限流模式

把 auth.py 现有 `_login_rate_key` 限流抽象为通用 `_rate_limiter`（进程内 dict + 窗口计数，键=IP），注册接口对「邀请码无效」计数，窗口超限 429。与登录限流同构、改动最小。

- **备选**：Redis 分布式限流 → 单 worker 下过度设计，保留进程内（与登录一致），多 worker 局限已在 auth 现有注释说明。
- **理由**：一致性 + 零依赖；邀请码 64bit 熵本身防单点爆破，限流主要用于防高频探测与资源消耗。

### D4. 登出清 cookie 收敛双通道残留

后端新增/改造登出（POST /api/auth/logout）调用 `response.delete_cookie(...)` 清除 httpOnly cookie；前端 logout 先调登出接口再清 localStorage。双通道并存本身保持（Non-Goals），但不再残留「后端会话仍有效」的缺口。

- **备选**：完全移除 localStorage 通道走 cookie-only → 属会话架构改造，移入 Non-Goals。

### D5. 队列/容量 CAS 修复遵循「状态门控 + rowcount 校验」统一模式

审查 B3/B4/B7/B10 及 quota_wait 冲突均属「CAS 更新无门控或 rowcount 未校验」同类问题。统一修复原则：
- 所有条件 UPDATE 的 WHERE 追加合法状态集（如 `status='pending'`），rowcount==0 视为冲突
- 冲突语义按端点定：写路径（sort/add/retry）→ 409「状态已变化」；准入内部（transfer）→ 标记 conflict 跳过，不误报 quota_wait
- 配套单测覆盖「并发推进后操作被拒/被正确识别」场景

### D6. pending_estimate 口径与前端活跃状态对齐

后端 `_pending_estimate_gb` 改查 DownloadQueue pending 行（SUM file_size），与 transfer 准入口径一致；前端 `DOWNLOAD_ACTIVE_STATUSES` 补 `'pending'` 与后端 `_DQ_ACTIVE` 对齐。纯口径/常量修正，无 API 形状变化。

### D7. 进程内缓存修复采用「有界队列入口」模式

EMBY/TMDB/POSTER 三处无界缓存（A1/A7/A9/D4/D11）统一改为容量上限 + 简单 LRU（`collections.OrderedDict` 移尾淘汰或 dict+deque）；海报缓存满后淘汰最旧再写入并校验 content-type（非 `image/*` 不入缓存返 502）。singleflight（D11）以 per-path `asyncio.Lock` 可选实现，属低优先。

### D8. 工程化：新增 `.github/workflows/ci.yml`

含 3 个 job：backend test（`pytest backend/tests`）、frontend test+build（`npm run test` + `npm run build`）、镜像构建冒烟（`docker build` + trivy 扫描可选）。CI 失败即拦截合并。trivy 依赖模型需确认（`.trivyignore` 已存在，沿用既有忽略清单）。

### D9. scan.py PG 到期过滤表达式修正（技术验证项）

现有 `bindparam("now") - (minutes * text("interval '1 minute'"))` 在 PG 上语义可疑。修复为 PostgreSQL 分支使用 `func.make_interval(secs=...)` 或 `literal_column("interval '1 minute'") * minutes` 的稳妥写法；**build 阶段先写最小 SQL 编译/渲染单测**确认双方言生成合法 SQL（SQLite 分支保持既有 datediff 写法）。A16 标记为 build 第一优先批次。

### D10. 首启调度器激活策略

lifespan 中 `system_config` 首次为空（首启引导）时，job 开关默认全部保持 paused；管理员配置凭据后由设置页显式开启。实现于 `scheduler._apply_job_switches`：空表时不再按默认 true 全开。

- **备选**：维持默认全开 → 无凭据时 scan/transfer 空转刷屏，否决。
- **风险**：已有部署升级后若 system_config 已有 job 开关键，行为不变（仅空表首启生效）。

## Risks / Trade-offs

- [外键 CASCADE 误删关联子行] → 仅对确认「子行随媒体删除而删除」的表声明 CASCADE（EpisodeState/TransferQueue/DownloadQueue/DownloadTask/TaskQueue）；引用类（requested_by/reviewed_by/used_by）用 SET NULL；迁移前以 `test_delete_media_fk.py` 现有用例验证删除语义无回归。
- [token_version 使在线长会话改密即踢] → 属预期安全语义；前端提示「修改密码后将重新登录」。
- [注册限流误伤合法新人] → 阈值宽松（与登录限流同级，如窗口 10 次），429 附带明确提示。
- [CI 首次接入因环境差异红] → 本地先跑通等价命令（pytest/vitest/build）再合并 workflow；`.trivyignore` 控制扫描噪音。
- [多 worker 容量告警冷却仍会重复推送] → 当前单 worker 部署不受影响；以注释 + 文档明示 trade-off（D 域 spec 已列为 ADDED 契约），不为此引入分布式状态。
- [单一大 change 验收面广] → tasks.md 按主题分组、每组独立勾选与测试闭环；CI 作为总闸。

## Migration Plan

1. **迁移 batch A（数据模型）**：写 alembic 迁移 → 升级/降级空跑验证 → PG 生产库执行前先在副本演练
2. **batch B（鉴权 + 配置/凭据）**：token_version、注册限流、logout 清 cookie、webhook 密钥遮蔽、admin 密码落盘——均为在线兼容改动（旧 token 在用户未改密前仍有效；遮蔽变化前端配套）
3. **batch C（队列/容量/前端契约）**：CAS 修复 + 口径对齐——无迁移，纯代码 + 测试
4. **batch D（缓存/调度/杂项 low）**：缓存有界化、CI workflow、scan 表达式、首启 job 策略
5. **回滚**：代码修复随 git revert 逐批回滚；alembic 降级脚本配套（CASCADE/SET NULL 可逆，类型/索引可逆）

## Open Questions

无阻塞设计的问题。build 阶段按需验证的技术项（不影响 spec/approach/tasks 分解）：
- A6 Emby `SeriesStatus` 大小写实际契约 → build 时以最小请求验证或按 `.capitalize()` 保守修复
- B8 下载完成回调端点现有鉴权状况 → build 时读 notify.py 确认，缺失则补鉴权
- 通知分页游标/页码形态 → 沿用现有 logs 分页的 offset 模式保持一致