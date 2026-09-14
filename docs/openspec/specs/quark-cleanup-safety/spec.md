# quark-cleanup-safety Specification

## Purpose
保护夸克网盘中正在被 aria2 下载或排队等待下载的源文件，避免孤儿清理等删除性操作误删下载源导致下载失败。

## Requirements

### Requirement: 清理前保护 aria2 下载中/等待下载源文件

系统执行夸克文件删除性操作（含孤儿文件兜底清理）前 SHALL 查询 aria2 活动任务与等待任务集合，解析每个任务下载的源文件（经下载 URI/文件路径归一为 /quark 下文件名），将命中集合的文件从待删除列表剔除；正在下载或排队等待下载的文件 MUST NOT 被删除。

#### Scenario: 下载中文件被保护
- **WHEN** 候选孤儿列表包含某文件，且该文件正被 aria2 活动任务下载（active）
- **THEN** 该文件不进入删除列表，其余未下载文件正常删除

#### Scenario: 等待下载文件被保护
- **WHEN** 候选孤儿列表包含某文件，且该文件对应 aria2 等待任务（waiting）
- **THEN** 该文件不进入删除列表

#### Scenario: 无下载任务时正常清理
- **WHEN** aria2 无活动/等待任务，或候选孤儿文件不在任何下载任务中
- **THEN** 系统按既有孤儿判定规则正常删除

### Requirement: aria2 状态查询失败时清理 fail-safe

清理执行前查询 aria2 活动/等待任务失败（服务不可用/网络故障）时，系统 SHALL 本次不删除任何孤儿文件并记录告警，不得在无法确认下载状态时执行删除。

#### Scenario: aria2 不可用时跳过清理
- **WHEN** 清理前查询 aria2 任务状态失败（Aria2Unavailable）
- **THEN** 本轮清理不删除任何文件，记录告警，下轮清理再试
