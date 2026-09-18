## Why

对全项目 5 个功能域（订阅/TMDB+Emby、队列/容量/转存、鉴权/审批/NasTools、日志/通知/设置/海报、核心/前端核心/部署/安全）进行「每个模块每个功能」全量代码审查后，共发现 75 个问题（1 critical + 5 high + ~20 medium + ~49 low）。这些问题涵盖安全缺陷（注册无限流、token 双通道、webhook 密钥明文）、功能回归（容量积压数据失真、前端漏 pending 状态、设置项保存必失败）、数据模型隐患（外键无级联、类型不一致）、资源性能问题（缓存无界、N+1 查询、表无限增长）与工程化红线（无 CI）。本 change 一次性修复全部审查发现的问题，消除安全与回归隐患，补齐工程化基础设施。

## What Changes

- **工程化（critical）**：新增 GitHub Actions CI（pytest + vitest + 前端构建 + 镜像构建门禁），使 70+ 测试自动运行
- **数据模型**：为子表外键声明 `ondelete` 级联策略；`TaskRun.media_id` 类型与 `Media.id` 对齐 BigInteger；为 `media_id` 外键列单独建索引（配套 alembic 迁移）
- **容量/队列前端契约**：`pending_estimate_gb` 改查 `DownloadQueue`（修复容量条积压数据失真）；前端「仅看活跃」补 `pending` 状态；sort/add_queue_task/retry 补齐 CAS 门控与 rowcount 校验
- **鉴权安全**：注册接口增加邀请码爆破限流；登录用户名枚举时序侧信道抹平；logout 清除 httpOnly cookie；登录 redirect 白名单校验；改密后吊销旧 token（token_version）
- **配置/凭据**：`download_queue_paused` 后端白名单与前端元数据对齐（保存不再 422）；webhook 鉴权密钥纳入敏感键遮蔽（GET `***`）；admin 初始密码不再明文刷日志（改落盘 600 权限文件）
- **Emby/TMDB**：Emby server_id/user_id 缓存随配置指纹失效；`status` 参数大小写与 Emby API 契约对齐；进程内缓存增加 LRU/淘汰上限；`refresh_episode_info` 批量 upsert 消除 N+1
- **通知/日志/海报**：站内通知增加分页与定期清理；PushPlus 失败降级站内告警；异常字符串脱敏；海报缓存满后淘汰 + 非图片响应不缓存；任务类型枚举改后端下发
- **调度/扫描**：scan.py PG 到期过滤表达式修正；首启无凭据时调度器默认不激活 job 防刷屏；PG 连接池显式配置；aria2 长故障不触发 recovery 回退循环
- **其余 low 级**：NaN 路由防御、死接口清理、前后端常量对齐、注释纠正、AsyncClient/连接复用等约 40 项

## Capabilities

### New Capabilities

- `auth-session`: 登录/注册/改密/登出的会话安全契约——注册限流、时序侧信道抹平、token 吊销（token_version）、logout 清 cookie、redirect 白名单。现有 specs 无独立鉴权能力，本次新增以承载上述行为约束。

### Modified Capabilities

- `emby-library-browse`: Emby 配置切换后封面/库列表/详情链接不再指向旧服务器；status 筛选大小写与 Emby API 契约对齐
- `media-detail-ui`: 非法媒体 id 路由防御（NaN 不再发起 `GET /api/media/NaN`）
- `media-pipeline`: scan 到期过滤 SQL 正确性、_enqueue 提交语义、NasTools 同步锁粒度、会话失效自动重登
- `notifications`: 站内通知分页与定期清理、PushPlus 失败降级站内告警、异常文案脱敏、容量告警冷却跨进程一致性
- `pipeline-admission`: 容量积压预估口径修正（查 DownloadQueue）、quota_wait CAS 冲突处理
- `pipeline-transfer`: add_queue_task 重置 CAS、cancel 已下载完成状态一致性、下载完成回调 gid 校验、aria2 故障不触发回退循环
- `queue-inspection-display`: 「仅看活跃」含 pending、sort 排序 CAS、retry 旧表分支 rowcount 校验
- `poster-proxy`: 非图片响应不缓存不代理（content-type 校验）、缓存满后淘汰
- `run-logs`: 任务类型筛选项由后端下发，不再前端硬编码
- `settings-credentials-ui`: download_queue_paused 前后端对齐、webhook 鉴权密钥遮蔽
- `user-invite-management`: 邀请上限前后端一致、邀请码生成数量约束

## Impact

- **代码**：backend `app/`（routers/auth.py、routers/settings.py、routers/capacity.py、routers/queue.py、routers/approvals.py、services/emby.py、services/tmdb.py、services/poster.py、services/notifier.py、services/pushplus.py、tasks/scan.py、tasks/transfer.py、tasks/cleanup.py、models/__init__.py、main.py、scheduler.py、database.py、config.py）、frontend `src/`（views/QueueView.vue、views/SettingsView.vue、views/LogsView.vue、stores、api/http.ts、types/index.ts、utils/format.ts、config/settingsMeta.ts）
- **数据**：alembic 新增迁移（外键级联、类型对齐、索引）
- **部署**：新增 `.github/workflows/ci.yml`；supervisord/compose 无破坏性变更
- **外部依赖**：无新增依赖；Emby/Aria2/NasTools 调用契约非破坏性对齐
- **不拆分原因**：用户已在决策点明确选择「全部合并一个 change」；75 个问题同源于一次全量审查，共享验证基础设施（CI、测试基线）与修复节奏，合并便于统一验收。修复任务按主题在 tasks.md 中分组管理，互不阻塞。