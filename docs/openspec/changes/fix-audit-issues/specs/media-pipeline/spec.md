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
