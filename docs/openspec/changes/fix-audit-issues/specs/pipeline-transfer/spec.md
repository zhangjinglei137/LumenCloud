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