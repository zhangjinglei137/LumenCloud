# Comet Spec Context

- Change: fix-audit-issues
- Phase: design
- Mode: beta
- Context hash: 7e20f78a13f23989cee3a441270cdf57a743e0ff12a609ca58740713d85a5022

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/fix-audit-issues/proposal.md
- SHA256: 5bfe9d6ade5749af0ba3b788ebdd293fd12eba00de4b160763329c5ecd31c610
- Source: docs/openspec/changes/fix-audit-issues/design.md
- SHA256: 5f0e47ec80d11ad4c28478adede0e814a1c9b5f39d4ec703028e064b152c0917
- Source: docs/openspec/changes/fix-audit-issues/tasks.md
- SHA256: d11fd8a4e406422d98e6b0fc7217c93f42af41b0ac385b8ae02e97ef318496e1
- Source: docs/openspec/changes/fix-audit-issues/specs/auth-session/spec.md
- SHA256: 6e58b259d98cf9891e5920c9145659e190a1adcf00659052585e19633255b822
- Source: docs/openspec/changes/fix-audit-issues/specs/emby-library-browse/spec.md
- SHA256: 1c6f9b4ae9956163aa4355206ea358f0a87f25a7a0b6fe8091a02bdbaca2afe6
- Source: docs/openspec/changes/fix-audit-issues/specs/media-detail-ui/spec.md
- SHA256: 7103f5709a3beba095dc03b2dc8004ef7a222a5eb4a54f3633b6888d3d504c0c
- Source: docs/openspec/changes/fix-audit-issues/specs/media-pipeline/spec.md
- SHA256: 03c0538ee0bb8764441f8e772791fda48e05f26be87e051f1bd310f4c8d4a918
- Source: docs/openspec/changes/fix-audit-issues/specs/notifications/spec.md
- SHA256: 788fe3fd369fc7edb333aad67be0a9aa15bc92d5e38507ad1e8a5e8d345fd21b
- Source: docs/openspec/changes/fix-audit-issues/specs/pipeline-admission/spec.md
- SHA256: 5df63ce37eef4ac6de02b282f153605ddc3689260625300987cdf3f3fee2e2f6
- Source: docs/openspec/changes/fix-audit-issues/specs/pipeline-transfer/spec.md
- SHA256: c4710ee04140e213dcbcba5bf08f2bfd65c5b021eac852a72634edbf43df40a8
- Source: docs/openspec/changes/fix-audit-issues/specs/poster-proxy/spec.md
- SHA256: 5d224b2bf165a96ba2629d8cedc05044a47217d53e66752079d0f9bfa76a1815
- Source: docs/openspec/changes/fix-audit-issues/specs/queue-inspection-display/spec.md
- SHA256: 4af75304cc360e963f848947a03fb2b4c145758305e44797c8cf7c4297bfa507
- Source: docs/openspec/changes/fix-audit-issues/specs/run-logs/spec.md
- SHA256: cb3017cda843fc5665652570d9a47c3327007b6beeb3e925f1b023961eb4f9e1
- Source: docs/openspec/changes/fix-audit-issues/specs/settings-credentials-ui/spec.md
- SHA256: c038806f435ef727fe4de3cc203b61a3bb37408afb7a7c1376f7648a085009e5
- Source: docs/openspec/changes/fix-audit-issues/specs/user-invite-management/spec.md
- SHA256: 45399aefd8456edab09c98ee9c44a2139e0b0f27609a6e0df6676fe716488056

## Acceptance Projection

## docs/openspec/changes/fix-audit-issues/specs/auth-session/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/auth-session/spec.md
- Lines: 1-53
- SHA256: 6e58b259d98cf9891e5920c9145659e190a1adcf00659052585e19633255b822

```md
## Purpose

定义登录/注册/改密/登出的会话安全契约：注册邀请码爆破限流、登录时序抹平、修改密码吊销既有令牌、登出清除服务端会话、登录跳转目标白名单，防止邀请码枚举、用户名枚举、会话残留与钓鱼跳转风险。

## ADDED Requirements

### Requirement: 注册接口邀请码爆破限流

注册接口 SHALL 按客户端对邀请码错误进行失败计数限流；窗口内连续失败达到阈值后 SHALL 返回 429 拒绝后续注册请求，不得允许无限次探测邀请码有效性。

#### Scenario: 邀请码连续错误触发限流
- **WHEN** 同一客户端在限流窗口内连续多次提交无效邀请码
- **THEN** 后续注册请求返回 429 并被提示稍后重试，无法持续探测邀请码

#### Scenario: 正常注册不受限流影响
- **WHEN** 客户端未触发失败阈值即提交有效邀请码
- **THEN** 注册正常完成，不返回限流错误

### Requirement: 登录响应时间一致

系统 SHALL 在用户名不存在时执行与存在用户等价的密码校验成本（如对固定 dummy 哈希执行校验），不得通过响应时间差异暴露用户名是否存在。

#### Scenario: 不存在用户响应时间与存在用户一致
- **WHEN** 攻击者提交不存在的用户名与任意密码
- **THEN** 响应时间与存在用户密码错误时相当，无法据时差枚举有效用户名

### Requirement: 修改密码吊销既有令牌

用户修改密码后 SHALL 使此前签发的令牌全部失效；旧令牌 SHALL 不再能访问受保护端点。

#### Scenario: 改密后旧令牌失效
- **WHEN** 用户修改密码后，使用改密前签发的令牌请求受保护端点
- **THEN** 请求返回 401，必须重新登录获取新令牌

### Requirement: 登出清除服务端会话

登出 SHALL 同时清除服务端 httpOnly cookie 会话与前端本地令牌；登出后浏览器残留的 cookie SHALL 不再能通过鉴权。

#### Scenario: 登出后令牌不再有效
- **WHEN** 用户点击登出
- **THEN** 前端本地令牌清除，服务端 cookie 被删除，使用残留 cookie 请求受保护端点返回 401

### Requirement: 登录跳转目标白名单

登录成功后的 redirect 参数 SHALL 仅接受站内相对路径（以 `/` 开头且不以 `//` 开头）；非法值（外部 URL、协议相对 URL）SHALL 回退到默认首页。

#### Scenario: 非法 redirect 回退
- **WHEN** 登录请求携带的外部地址（如 `https://evil.com`）作为 redirect
- **THEN** 登录后跳转默认首页，不跳转到外部地址

#### Scenario: 合法站内 redirect 生效
- **WHEN** 登录请求携带以 `/` 开头的站内相对路径作为 redirect
- **THEN** 登录后跳转到该站内路径

```

## docs/openspec/changes/fix-audit-issues/specs/emby-library-browse/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/emby-library-browse/spec.md
- Lines: 1-25
- SHA256: 1c6f9b4ae9956163aa4355206ea358f0a87f25a7a0b6fe8091a02bdbaca2afe6

```md
## ADDED Requirements

### Requirement: 配置变更后连接缓存失效

管理员修改 Emby 服务地址等连接配置后，系统 SHALL 使已缓存的 Emby server_id / user_id 等连接态缓存失效并重新获取，详情链接、库列表等 SHALL 指向新配置的服务器，不得继续指向旧服务器。

#### Scenario: 切换 Emby 服务器后指向新服务器
- **WHEN** 管理员修改 Emby 基础地址配置且新服务器可用
- **THEN** 后续详情链接与库列表基于新服务器生成，不再使用旧服务器 id

#### Scenario: 未变更配置时缓存复用
- **WHEN** Emby 连接配置未变化
- **THEN** 既有连接缓存正常复用，不额外发起重取

### Requirement: 状态筛选与 Emby 契约对齐

Emby 影视库状态筛选（在更/完结）参数 SHALL 与 Emby API 契约保持一致（含大小写）；筛选值不匹配 SHALL 导致对应状态条目正确返回，不得因参数形态不符而静默失效。

#### Scenario: 在更筛选返回在更条目
- **WHEN** 用户在 Emby 影视库选择「在更」状态筛选
- **THEN** 返回结果包含在更状态条目，筛选生效

#### Scenario: 完结筛选返回完结条目
- **WHEN** 用户在 Emby 影视库选择「完结」状态筛选
- **THEN** 返回结果包含完结状态条目，筛选生效

```

## docs/openspec/changes/fix-audit-issues/specs/media-detail-ui/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/media-detail-ui/spec.md
- Lines: 1-13
- SHA256: 7103f5709a3beba095dc03b2dc8004ef7a222a5eb4a54f3633b6888d3d504c0c

```md
## ADDED Requirements

### Requirement: 非法媒体标识防御

影视详情页 SHALL 在路由参数不是合法媒体 id（非数字或 NaN）时不发起后端详情请求，并回退到影视列表页或展示可操作的空态，不得向 `GET /api/media/<非法值>` 发起请求。

#### Scenario: 非法路由参数回退列表
- **WHEN** 用户访问 `/media/abc` 等非数字媒体 id 路由
- **THEN** 页面不发起非法详情请求，并跳转到影视列表页或展示可操作空态

#### Scenario: 合法媒体 id 正常加载
- **WHEN** 路由参数为合法数字媒体 id
- **THEN** 详情页正常加载该影视数据

```

## docs/openspec/changes/fix-audit-issues/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/media-pipeline/spec.md
- Lines: 1-41
- SHA256: 03c0538ee0bb8764441f8e772791fda48e05f26be87e051f1bd310f4c8d4a918

```md
## ADDED Requirements

### Requirement: 巡检到期过滤 SQL 正确性

巡检「到期影视」判定使用的 SQL 表达式 SHALL 在目标数据库（PostgreSQL/SQLite）上生成合法且语义正确的查询；interval 算术不得依赖不确定的表达式构造（如 Column 与 TextClause 混乘），防止到期过滤静默失效或运行时错误导致巡检 tick 异常。

#### Scenario: 到期过滤在 PG 上正确执行
- **WHEN** 系统运行于 PostgreSQL 且执行巡检到期判定
- **THEN** 到期判定 SQL 正确生成并返回预期影视集合，不抛运行时错误

#### Scenario: 到期过滤在 SQLite 上正确执行
- **WHEN** 系统运行于 SQLite（测试/单机）且执行巡检到期判定
- **THEN** 到期判定行为与 PostgreSQL 一致，不因方言差异失效

### Requirement: 入库任务入队提交语义一致

入库（巡检）任务入队写库 SHALL 使用明确的提交语义：同一入队流程的事务提交/回滚 SHALL 由上下文管理器统一管理，不得依赖显式 commit 与上下文退出的版本耦合行为；唯一键冲突导致的入队失败 SHALL 被捕获并正确降级（跳过该条，不中断整轮巡检）。

#### Scenario: 入队冲突单条跳过
- **WHEN** 入队某集任务时撞唯一键约束（如该集已入队）
- **THEN** 该条跳过，巡检流程继续处理其余缺失集，不中断

### Requirement: NasTools 会话失效自动重登

系统调用 NasTools 同步接口时 SHALL 在会话失效（401/403）时自动清除过期会话并重新登录后重试一次；不得在会话过期后持续失败直至进程重启。

#### Scenario: 会话过期自动重登
- **WHEN** NasTools 会话已过期且系统发起目录同步
- **THEN** 系统自动重新登录并完成该次同步，不因会话过期持续失败

#### Scenario: 重登仍失败时上抛错误
- **WHEN** 重新登录后同步仍失败
- **THEN** 系统按既有失败语义上抛 NasTools 不可用错误，不无限重试

### Requirement: NasTools 同步冷却检查不阻塞刮削

NasTools 同步互斥锁 SHALL 仅保护冷却检查与时间戳更新等短临界区；长等待（如冷却睡眠）SHALL 不持有锁，避免下载完成触发的刮削同步被日常兜底同步长时间阻塞。

#### Scenario: 刮削触发不被兜底同步长阻塞
- **WHEN** 日常兜底同步正在执行且下载完成触发刮削同步
- **THEN** 刮削同步在兜底同步的长等待（冷却睡眠）期间不被锁阻塞，可及时响应

```

## docs/openspec/changes/fix-audit-issues/specs/notifications/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/notifications/spec.md
- Lines: 1-45
- SHA256: 788fe3fd369fc7edb333aad67be0a9aa15bc92d5e38507ad1e8a5e8d345fd21b

```md
## ADDED Requirements

### Requirement: 站内通知分页

站内通知列表 SHALL 支持分页（limit/offset）并返回总条数；通知接口不得一次性返回全部历史通知，避免长期累积后单次查询与响应体积无界增长。

#### Scenario: 通知列表分页返回
- **WHEN** 用户通知数量超过单页上限
- **THEN** 通知接口返回当前页数据与总条数，前端可翻页浏览

### Requirement: 站内通知定期清理

系统 SHALL 对站内通知执行定期清理（如按创建时间保留 N 天），防止 notifications 表无限增长；已读/历史通知超出保留期 SHALL 被清除。

#### Scenario: 过期通知被清理
- **WHEN** 定期清理任务运行且存在超过保留期的通知记录
- **THEN** 超期通知被删除，表体积受控

#### Scenario: 保留期内通知不受影响
- **WHEN** 定期清理任务运行且通知未超过保留期
- **THEN** 通知保留不变

### Requirement: PushPlus 推送失败降级站内告警

PushPlus 通道推送失败时，系统 SHALL 向站内通知补发一条「推送失败」告警（或记录到可观察的任务记录），使用户能感知 PushPlus 通道失效；不得静默吞掉失败。

#### Scenario: 推送失败产生站内告警
- **WHEN** PushPlus token 已配置但推送接口返回失败
- **THEN** 系统产生一条站内通知告警，提示 PushPlus 推送失败

### Requirement: 通知文案脱敏

通知正文 SHALL 对异常/错误信息进行截断与脱敏（去除 URL 中的 userinfo 与 query token、不原样透传含凭据的原始异常字符串），MUST NOT 通过站内通知或 PushPlus 泄露服务凭据。

#### Scenario: 异常文案不含凭据
- **WHEN** NasTools 等外部服务异常信息包含 URL 或请求详情
- **THEN** 通知正文仅包含错误类型/状态码等安全信息，不含凭据或敏感 query

### Requirement: 容量告警冷却跨进程一致

容量告警的冷却（去抖）状态 SHALL 不依赖单进程内存状态即可保持基本一致；多 worker 部署下不得因每个进程独立冷却导致告警成倍重复推送。若保留进程内冷却，SHALL 以配置注释明示多 worker 下的重复推送 trade-off。

#### Scenario: 多 worker 不重复告警
- **WHEN** 系统以多 worker 部署且容量连续超阈值
- **THEN** 告警推送次数与单 worker 一致（或差异有明确定义），不按 worker 数倍放

```

## docs/openspec/changes/fix-audit-issues/specs/pipeline-admission/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/pipeline-admission/spec.md
- Lines: 1-29
- SHA256: 5df63ce37eef4ac6de02b282f153605ddc3689260625300987cdf3f3fee2e2f6

```md
## ADDED Requirements

### Requirement: 容量积压预估口径正确

容量状态的「未消费积压」（pending 预估）SHALL 基于当前生效的下载队列模型统计（DownloadQueue 中 pending 行），不得查询已废弃的旧表；积压预估口径 SHALL 与前端容量条展示语义一致，不得长期恒为 0 或失真。

#### Scenario: 积压预估反映真实排队
- **WHEN** 下载队列存在 pending（等待准入）任务且容量接口返回积压预估
- **THEN** 积压预估值包含这些 pending 任务的体积合计，与真实排队一致

### Requirement: 等待准入冲突处理

下载队列准入流程中，若并发方已将任务从 pending 推进（CAS 未命中），系统 SHALL 将该冲突显式记为并发冲突并跳过该任务，不得按「容量不足」错误语义处理导致误报配额告警或错误状态返回。

#### Scenario: 并发推进后不误报配额等待
- **WHEN** 准入尝试时任务已被并发方推进出 pending（CAS 未命中）
- **THEN** 系统将本次视为冲突跳过，不返回「配额等待」并停止本批准入的错误语义

### Requirement: 手动加集不打断探测中任务

手动向下载队列添加剧集时，SHALL 对既有任务行加状态门控：仅允许重置终态/可重置态任务，探测中（probing）等运行中状态 SHALL 不被重置；重置不得破坏探测结果落库的一致性。

#### Scenario: 探测中任务不被重置
- **WHEN** 某行处于 probing（探测中）时收到手动加集请求
- **THEN** 该行保持探测状态不被重置为 pending，探测结果正常落库

#### Scenario: 终态任务可重置
- **WHEN** 某行处于终态（failed/skipped 等）且收到手动加集请求
- **THEN** 该行可被重置入队，不报错

```

## docs/openspec/changes/fix-audit-issues/specs/pipeline-transfer/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/pipeline-transfer/spec.md
- Lines: 1-52
- SHA256: c4710ee04140e213dcbcba5bf08f2bfd65c5b021eac852a72634edbf43df40a8

```md
## ADDED Requirements

### Requirement: 下载完成回调校验来源

下载完成回调（trigger_download_complete）SHALL 校验回调来源：除按 gid 反查任务外，SHALL 确认回调持有合法鉴权信号（如 webhook 密钥/系统令牌），且 gid 对应本系统已签发的下载任务；无法通过鉴权或 gid 未匹配本次任务的回调 SHALL 被拒绝，不得推进任意任务状态。

#### Scenario: 非法回调被拒绝
- **WHEN** 回调请求未通过鉴权或 gid 不属于本系统签发任务
- **THEN** 请求被拒绝（401/404），下载任务状态不被推进

#### Scenario: 合法回调正常推进
- **WHEN** 回调通过鉴权且 gid 匹配系统已签发下载任务
- **THEN** 任务按既有逻辑推进到刮削/转移阶段

### Requirement: 下载跟踪与 aria2 故障解耦

下载中任务的超时恢复 SHALL 区分「aria2 服务不可用」与「任务长时间无进展」：aria2 长时间故障期间 SHALL 不触发下载中任务回退重置（避免反复重新转存耗尽重试计数）；aria2 恢复后可正常继续跟踪。

#### Scenario: aria2 故障不触发回退循环
- **WHEN** aria2 服务持续不可用且下载中任务超时
- **THEN** 任务不因 aria2 故障被回退并重复转存，重试计数不被无谓消耗

#### Scenario: aria2 恢复后继续跟踪
- **WHEN** aria2 服务恢复正常
- **THEN** 下载中任务按既有状态轮询继续推进

### Requirement: 取消已下载完成任务不误标失败

用户取消下载中任务 SHALL 先确认 aria2 实际状态：若任务在 aria2 中已实际下载完成（仅状态回写滞后），取消操作 SHALL 不将其标为 failed，而按已完成路径推进；aria2.remove 失败 SHALL 不静默吞掉完成态。

#### Scenario: 已下载完成的任务取消后正常完成
- **WHEN** 用户取消某任务但 aria2 中该任务已下载完成
- **THEN** 任务按下载完成路径推进，不被误标 failed

#### Scenario: 未完成的取消正常失败
- **WHEN** 用户取消正在下载且 aria2 中确实未完成的任务
- **THEN** 任务被正常标记 failed/取消，清理 aria2 任务

### Requirement: 重试操作反馈真实结果

下载队列重试操作 SHALL 校验目标行当前状态（CAS），状态已变化的行（如已被并发推进）SHALL 返回明确冲突提示，不得静默「成功」而实际未重置。

#### Scenario: 状态已变更时重试返回冲突
- **WHEN** 用户重试某任务但其状态在请求前已被并发推进
- **THEN** 系统返回明确冲突提示（如 409 状态已变化），不假报成功

### Requirement: 排序操作不污染在途任务

下载队列排序（上移/下移）SHALL 对目标行加状态门控：仅允许排序 pending 状态行；已被推进为在途（transferring/downloading 等）的行 SHALL 不被修改排序键。

#### Scenario: 在途任务不被改序
- **WHEN** 并发方将某 pending 行推进为 transferring 后收到排序请求
- **THEN** 该行排序键不被修改，在途任务不受影响
```

## docs/openspec/changes/fix-audit-issues/specs/poster-proxy/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/poster-proxy/spec.md
- Lines: 1-20
- SHA256: 5d224b2bf165a96ba2629d8cedc05044a47217d53e66752079d0f9bfa76a1815

```md
## ADDED Requirements

### Requirement: 海报代理缓存满后淘汰

海报代理进程内缓存 SHALL 在达到容量上限后淘汰旧条目（LRU/最旧优先）再写入新条目，不得因缓存满导致缓存实质失效（新内容不再缓存）。

#### Scenario: 缓存满仍可写入新条目
- **WHEN** 海报代理缓存达到上限且请求新海报
- **THEN** 新海报正常缓存（淘汰最旧条目），缓存继续命中后续请求

### Requirement: 海报代理校验响应类型

海报代理回源响应 SHALL 校验 Content-Type 为图片类型后才缓存并返回；非图片响应（HTML 错误页等）SHALL 不被缓存，并按失败语义返回（502/降级），不得以错误类型内容污染缓存。

#### Scenario: 非图片响应不缓存
- **WHEN** 上游图床返回 200 但 Content-Type 非图片（如 HTML 错误页）
- **THEN** 代理不缓存该内容并按失败语义处理（如 502），前端走海报占位兜底

#### Scenario: 正常图片响应缓存
- **WHEN** 上游返回合法图片 Content-Type
- **THEN** 代理正常缓存并返回图片内容
```

## docs/openspec/changes/fix-audit-issues/specs/queue-inspection-display/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/queue-inspection-display/spec.md
- Lines: 1-8
- SHA256: 4af75304cc360e963f848947a03fb2b4c145758305e44797c8cf7c4297bfa507

```md
## ADDED Requirements

### Requirement: 仅看活跃包含排队中任务

下载队列「仅看活跃」筛选 SHALL 与后端活跃状态集一致，包含 pending（等待准入/排队中）任务；pending 任务不得被「仅看活跃」过滤消失。

#### Scenario: 仅看活跃展示排队任务
- **WHEN** 用户开启下载队列「仅看活跃」且存在 pending 任务
- **THEN** pending 任务仍显示（属活跃排队态），不消失
```

## docs/openspec/changes/fix-audit-issues/specs/run-logs/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/run-logs/spec.md
- Lines: 1-8
- SHA256: cb3017cda843fc5665652570d9a47c3327007b6beeb3e925f1b023961eb4f9e1

```md
## ADDED Requirements

### Requirement: 任务类型筛选列表由后端下发

运行日志页的任务类型筛选选项 SHALL 由后端提供（动态下发当前实际类型枚举），前端不得硬编码任务类型列表；后端新增任务类型后筛选下拉 SHALL 自动出现对应选项。

#### Scenario: 筛选选项与后端实际类型一致
- **WHEN** 后端存在运行日志数据且用户展开任务类型筛选
- **THEN** 下拉选项与后端实际产生的任务类型一致，无需前端随新增类型同步改版
```

## docs/openspec/changes/fix-audit-issues/specs/settings-credentials-ui/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/settings-credentials-ui/spec.md
- Lines: 1-24
- SHA256: c038806f435ef727fe4de3cc203b61a3bb37408afb7a7c1376f7648a085009e5

```md
## ADDED Requirements

### Requirement: 业务开关保存成功

设置页各业务开关（含 download_queue_paused）SHALL 能成功保存：前端渲染的每个可编辑/可切换键 SHALL 均在后端可编辑白名单内，保存后 PATCH 成功并生效，不得出现「前端可切换但保存返回 422」的失效状态。

#### Scenario: 队列暂停开关保存成功
- **WHEN** 管理员在设置页切换「下载队列暂停」开关
- **THEN** PATCH 保存成功（非 422），开关状态持久化并生效

#### Scenario: 其余业务开关保存正常
- **WHEN** 管理员修改设置页任意可编辑业务参数
- **THEN** 保存成功且立即生效，不因白名单缺失返回 422

### Requirement: 回调鉴权密钥遮蔽

回调鉴权密钥（如 aria2 webhook secret、NasTools webhook token）SHALL 视同敏感凭据：GET 设置接口不得明文回显（以 `***` 占位），前端凭据表单对应字段 SHALL 以密码框/「已配置」形式呈现，保存时留空表示不修改。

#### Scenario: 回调密钥不回显明文
- **WHEN** 管理员打开设置页查看服务凭据
- **THEN** 回调鉴权密钥字段显示 `***` 占位（或「已配置」），不返回明文

#### Scenario: 回调密钥留空不修改
- **WHEN** 凭据表单中回调鉴权密钥字段留空保存
- **THEN** 已配置的密钥保持不变，不被空值覆盖
```

## docs/openspec/changes/fix-audit-issues/specs/user-invite-management/spec.md

- Source: docs/openspec/changes/fix-audit-issues/specs/user-invite-management/spec.md
- Lines: 1-20
- SHA256: 45399aefd8456edab09c98ee9c44a2139e0b0f27609a6e0df6676fe716488056

```md
## ADDED Requirements

### Requirement: 邀请码生成上限前后端一致

邀请码一次生成的数量上限 SHALL 在前端校验与后端校验保持一致（上限值以常量统一定义）；前端不得限制低于后端允许值导致部分合法数量无法通过 UI 生成。

#### Scenario: UI 可生成后端允许的全部数量
- **WHEN** 管理员在用户管理页指定后端允许范围内的邀请码数量
- **THEN** UI 允许输入并成功生成该数量，不因前端上限低于后端而受限

### Requirement: 注册接口邀请码限流

用户注册接口 SHALL 对邀请码验证失败进行限流（按客户端计数），防止大规模探测邀请码有效性；失败窗口超限 SHALL 返回 429。限流 SHALL 不影响正常注册流程。

#### Scenario: 邀请码探测被限流
- **WHEN** 同一客户端在窗口中连续提交无效邀请码
- **THEN** 后续注册请求返回 429，邀请码无法被持续探测

#### Scenario: 正常注册不受影响
- **WHEN** 客户端未触发失败阈值即提交有效邀请码
- **THEN** 注册正常完成
```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.