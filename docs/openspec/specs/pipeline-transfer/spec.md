# pipeline-transfer Specification

## Purpose
为影视下载提供转移与入库闭环：下载完成后交给 NasTools 转移整理，以 Webhook 完成信号触发 Emby 媒体库扫描，确认入库后标记完成并释放容量，继续消费下载队列等待任务。

## Requirements

### Requirement: 下载完成进入转移

下载任务完成后，系统 SHALL 通过 NasTools 完成转移整理；以 nastools Webhook（transfer.finished）或其他等价信号判断转移是否完成。

#### Scenario: 判定转移完成
- **WHEN** 下载任务完成且收到 NasTools 转移完成 Webhook
- **THEN** 系统推进该任务进入入库确认阶段

### Requirement: 转移完成后触发 Emby 扫描

系统 SHALL 在判定转移完成后调用 Emby 媒体库扫描（对应媒体库文件夹），确保新文件被 Emby 及时收录。

#### Scenario: 转移完成触发扫描
- **WHEN** NasTools 报告某影视转移完成
- **THEN** 系统调用 Emby 扫描媒体库接口触发该媒体库刷新

#### Scenario: 扫描失败回退轮询确认
- **WHEN** Emby 扫描调用失败或不可达
- **THEN** 系统保留任务在入库确认状态，由后续轮询兜底再扫描并确认

### Requirement: 入库确认与容量释放

任务在 Emby 确认收录后 SHALL 标记完成，删除夸克中转文件并释放容量预留，随后继续消费下载队列等待任务（重复容量判断流程）。

#### Scenario: 入库成功完成闭环
- **WHEN** Emby 已确认收录该集/该片
- **THEN** 任务标记完成、删除夸克中转、释放容量预留，并触发下载队列继续按序取件

#### Scenario: 入库超时处理
- **WHEN** 任务在入库确认状态停留超过超时阈值仍未被 Emby 收录
- **THEN** 系统按回退策略处理（重试或失败），不永久卡驻队列
