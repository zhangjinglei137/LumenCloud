## 1. 工程化：CI 与部署基线

- [ ] 1.1 新增 `.github/workflows/ci.yml`（backend pytest + frontend vitest/build + Docker 构建冒烟三 job），本地先跑通等价命令后合并，验证 push 后 CI 全绿
- [ ] 1.2 为 `json.py` 的 `install_zulu_encoder` 增加 fastapi 版本守卫或回归断言（沿用 test_json_encoder），验证升级后序列化行为有测试封闭

## 2. 数据模型与迁移

- [x] 2.1 编写单一 alembic 迁移 `audit_fixes`：为 EpisodeState/TransferQueue/DownloadQueue/DownloadTask/TaskQueue 等子表外键声明 `ondelete=CASCADE`，引用类（WatchRequest.requested_by/reviewed_by、InviteCode.used_by）声明 `SET NULL`；升级/降级成对，验证 `alembic upgrade/downgrade` 空跑通过
- [x] 2.2 将 `TaskRun.media_id` 类型对齐为 BigInteger（`BIG_PK`），验证 PG 建表类型一致
- [x] 2.3 为 EpisodeState/TaskQueue/TransferQueue 等缺失的 `media_id` 外键列补独立索引，验证 EXPLAIN 按 media_id 过滤走索引
- [x] 2.4 前端 `MediaItem.tmdb_id` 类型改为 `number | null` 与后端 nullable 契约对齐，验证 `npm run build` 类型检查通过
- [x] 2.5 运行 `test_delete_media_fk.py` 及媒体删除相关测试，验证 CASCADE 改动后删除语义无回归

## 3. 鉴权与会话安全（auth-session 能力）

- [x] 3.1 抽象登录限流为通用限流器，注册接口对邀请码错误计数限流（窗口超限 429），验证新增注册限流测试通过且正常注册不受影响
- [x] 3.2 登录用户不存在时对 dummy hash 执行等价校验抹平时序侧信道，验证新增时序等价测试通过
- [x] 3.3 users 表新增 `token_version`，JWT payload 带 `ver`、`get_current_user` 校验，改密时递增，验证改密后旧 token 返回 401
- [x] 3.4 新增/改造登出接口清除 httpOnly cookie，前端 logout 先调登出再清 localStorage，验证登出后残留 cookie 无法通过鉴权
- [x] 3.5 登录 redirect 参数白名单校验（`/` 开头且非 `//`），非法回退首页，验证新增跳转安全测试通过
- [x] 3.6 并发审批同 tmdb_id 捕获 IntegrityError 返回 409（approvals.py），验证新增并发审批测试无 500
- [x] 3.7 admin 初始密码改为一次性 token 落盘 `data/` 下 chmod 600 文件（或等价方案），不再明文刷日志，验证启动日志不含密码且文件权限正确
- [ ] 3.8 审查并确认登录限流反代头信任链（或注释明示单 worker/反代局限），验证配置文档更新

## 4. 配置/凭据/设置

- [ ] 4.1 后端 `_WHITELIST_EXACT` 补 `download_queue_paused`（且不进 `_EDITABLE_KEYS` 凭据表单），验证设置页保存该开关返回 200 且立即生效
- [x] 4.2 将 `internal_aria2_webhook_secret`、`internal_nastools_webhook_token` 纳入 `_SENSITIVE_KEYS` 遮蔽，前端表单改密码框/「已配置」占位、留空不修改，验证 GET 不回显明文且保存不覆盖
- [ ] 4.3 邀请码生成上限前后端统一（后端 50、前端同步），验证 UI 可生成后端允许全部数量
- [ ] 4.4 移除 pushplus.py 模块级单例或使单例每次重读 token，验证配置热更新后新通知使用新 token

## 5. 队列/容量/转存（pipeline 系能力）

- [x] 5.1 `_pending_estimate_gb` 改查 DownloadQueue pending 行 SUM(file_size)，验证容量接口积压预估反映真实排队
- [ ] 5.2 前端 `DOWNLOAD_ACTIVE_STATUSES` 补 `'pending'` 与后端 `_DQ_ACTIVE` 对齐，验证「仅看活跃」展示排队任务
- [ ] 5.3 sort_task 上/下移加 `status='pending'` CAS 门控，rowcount==0 返回 409，验证并发推进后排序被拒
- [ ] 5.4 add_queue_task 重置加状态门控（probing 等运行态不被重置），验证探测中任务不被重置
- [ ] 5.5 cancel_task 对 downloading 行先 `tell_status` 确认非 complete 再标 failed（complete 则走完成路径），验证新增取消已完成任务测试
- [ ] 5.6 `_try_admit_one` quota_wait 分支 CAS 未命中时设 conflict（与抢占分支一致），验证并发推进后不误报 quota_wait
- [ ] 5.7 retry_task 旧三表分支检查 rowcount，==0 返回 409「状态已变化」，验证新增冲突测试
- [ ] 5.8 下载完成回调端点补鉴权与 gid 维度校验（确认 notify.py 现状后），验证非法回调被拒测试
- [ ] 5.9 recovery 回退前区分「aria2 故障」与「任务无进展」（aria2 探活），验证 aria2 故障期间不触发回退循环
- [ ] 5.10 容量告警冷却：注释+文档明示单 worker/多 worker trade-off（或 DB 落冷却），验证告警逻辑不受影响
- [ ] 5.11 probe_media 加 per-media 频率限制或 scan 内去重，验证连续 probe 被限流

## 6. Emby/TMDB 一致性与缓存（emby-library-browse 等能力）

- [ ] 6.1 Emby server_id/user_id 全局缓存键附加 `_config_fingerprint()`（或配置变更时重置），验证切换配置后详情/库列表指向新服务器
- [ ] 6.2 `_build_library_params` status 值按 Emby 契约对齐大小写（验证后 `.capitalize()` 或映射），验证在更/完结筛选生效
- [ ] 6.3 tmdb.py `_SEASON_AIR_CACHE`/`_ALL_EPS_CACHE` 加容量上限+LRU 淘汰，验证长跑内存受控（单测覆盖淘汰）
- [ ] 6.4 `refresh_episode_info` 批量 upsert 消除 N+1（PG ON CONFLICT 或 bulk），验证相同数据批量/逐条结果一致且查询数下降
- [ ] 6.5 emby `_INGESTED_CACHE`/`_recent_empty_check` 有界化（与 6.3 同模式），验证淘汰行为
- [ ] 6.6 library_check 批量预取 media 减少 N+1 session，验证行为一致
- [ ] 6.7 `list_all_library` 两段式赋值冗余精简（先取 server_id 再归一化），验证 emby_web_url 结果一致

## 7. 通知/日志/海报（notifications/poster-proxy/run-logs 能力）

- [ ] 7.1 站内通知列表加分页（limit/offset + total），前端铃铛用首页切片，验证分页测试通过
- [ ] 7.2 清理任务新增 notifications 保留期清理（沿用 prune_history 模式），验证超期通知被删除、保留期内不受影响
- [ ] 7.3 PushPlus 推送失败补发站内「推送失败」告警（或记录可观察 task_run），验证失败时产生站内通知
- [ ] 7.4 通知异常文案截断与脱敏（去 URL userinfo/query token），验证通知正文不含凭据
- [ ] 7.5 海报缓存满后淘汰最旧（LRU）再写入 + 回源响应 content-type 校验（非 `image/*` 不缓存返 502），验证新增海报缓存/类型测试
- [ ] 7.6 海报缓存 per-path singleflight（可选，asyncio.Lock 按 path 节流），验证并发同键只回源一次
- [ ] 7.7 运行日志任务类型筛选项改后端下发（新增 `/api/logs/task-types` 或随列表返回枚举），前端动态渲染，验证后端新增类型后选项自动出现

## 8. 调度/扫描/杂项

- [ ] 8.1 scan.py PG 到期过滤表达式修正（`func.make_interval`/`literal_column`），先写双方言 SQL 渲染单测，验证 PG 分支生成合法 SQL
- [ ] 8.2 `_enqueue` 移除显式 `tx.commit()`、统一由上下文管理器管理（冲突捕获降级单条跳过），验证入队冲突不中断整轮巡检
- [ ] 8.3 NasTools `_do` 收到 401/403 清除 session cookie 并重登一次（限一次防循环），验证会话过期自动重登测试
- [ ] 8.4 nastools_sync `_sync_lock` 缩小临界区（冷却检查+时间戳互斥，sleep 移出锁外），验证刮削触发不被兜底同步长阻塞
- [ ] 8.5 `trigger_emby_refresh()` 移出 `if advanced:` 块（整理完成事件即触发 Refresh），验证非 advanced 场景也会触发扫描
- [ ] 8.6 首启（system_config 空表）job 开关默认 paused，管理员配置后显式开启，验证首启不激活 scan/transfer 空转
- [ ] 8.7 PG 连接池显式配置（pool_size/max_overflow/pool_recycle），验证并发峰值下无连接耗尽
- [ ] 8.8 `main.py` 日志挂载改为 lifespan 内显式 handler 配置（不依赖 uvicorn 内部行为），验证启动日志行为不变
- [ ] 8.9 `serve_spa` 对 `/internal/*` 未注册路径返回 404 JSON（与 /api 一致），验证新增 404 测试
- [ ] 8.10 `test_scheduler.py` 过时注释「7 个固定 job」改为 10，验证注释与实现一致
- [ ] 8.11 登录 redirect 前端跳转体验：评估 `toLogin` 统一为 router.replace（保留 SPA 状态）或记录现状，验证 401 拦截行为一致

## 9. 前端低风险防御

- [ ] 9.1 MediaDetailView 路由参数 `Number.isFinite` 守卫（非法回退列表页），验证 `/media/abc` 不发非法请求
- [ ] 9.2 TmdbSearch `search()` 增加错误态标记（searched 错误分支），验证搜索失败有内联反馈
- [ ] 9.3 QueueView 计时器按 activeTab watch 启停（progressTimer 切 Tab 暂停），验证切 Tab 无空转请求
- [ ] 9.4 前端 `taskTypes` 硬编码移除（承接 7.7 后端下发），验证 LogsView 筛选项动态渲染
- [ ] 9.5 admin PATCH 用户角色死接口：补前端入口或删除端点（按产品决策，文档标记），验证无漂移残留
- [ ] 9.6 tmdb_id 为 None 时审批查重以 title 模糊兜底（或要求必填），验证无 tmdb_id 防重复提交
- [ ] 9.7 补充 QueueView 控制面操作（cancel/prioritize/sort/retry）交互测试与路由守卫/http 401 单测，验证关键交互路径有覆盖

## 10. 全量验证与收尾

- [ ] 10.1 运行全部 backend pytest 与 frontend vitest，验证全绿且无新增回归
- [ ] 10.2 对照审查清单逐条核对 75 个问题的修复状态，验证无遗漏项（low 级确认接受或修复）
- [ ] 10.3 运行 `npm run build` + Docker 镜像构建冒烟（本地），验证前端产物与容器启动正常
- [ ] 10.4 CI workflow 在推送后实际运行并全绿，验证自动回归闸门生效