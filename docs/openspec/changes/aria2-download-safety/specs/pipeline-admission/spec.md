## MODIFIED Requirements

### Requirement: 转存来源校验与逃生

系统在准入转存前 SHALL 校验 aria2 活动/等待任务的来源；aria2 中的陌生 gid（不属于本系统 download_queue 已签发集合）不得导致转存拦截、告警或自动清理删除——用户自行添加的下载任务 SHALL 与系统任务共存。系统 SHALL 仅对自有已签发 gid 执行下载状态跟踪与完成推进。

#### Scenario: 陌生任务共存不拦截
- **WHEN** aria2 中存在 gid 不在本系统已签发集合的活动/等待任务（如用户自行下载）
- **THEN** 转存流程不受影响正常继续，不产生非本系统任务告警，不删除该任务

#### Scenario: 本系统任务正常跟踪
- **WHEN** aria2 中的任务 gid 属于本系统已签发集合
- **THEN** 系统按既有状态轮询与完成推进逻辑处理
