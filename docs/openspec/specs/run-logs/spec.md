# run-logs Specification

## Purpose
运行日志页面提供任务执行记录的可读展示与生命周期管理：任务类型中文映射、基于后端真实 total 的分页，以及日志保留天数动态配置。

## Requirements

### Requirement: 任务类型中文映射

系统 SHALL 将运行日志中后端实际写入的任务类型（含 `scan_media`、`scan_all_media`、`transfer`、`cleanup`、`capacity_alert`、`recover`、`sync_nastools`、`notify`、`prune_history`）映射为中文标签展示；未登记类型 SHALL 保留原值并按未知类型样式展示，不得显示为英文专名或空值。

#### Scenario: 已知类型显示中文
- **WHEN** 运行日志存在 `sync_nastools` / `notify` / `prune_history` 类型的记录
- **THEN** 列表任务类型列分别显示「目录同步入库」/「通知」/「历史清理」等中文标签，而非英文原值

#### Scenario: 未知类型回退原值
- **WHEN** 记录的任务类型未在映射表中登记
- **THEN** 展示该类型原值，并以未知类型样式（如灰色）标识

### Requirement: 任务类型筛选只含有效类型

系统 SHALL 仅在运行日志筛选下拉中列出后端实际产生的任务类型（含历史保留记录的类型），不得包含已不产生的历史别名（如 `media_scan`、`recovery`、`transfer_retry`、`download` 若不再写入）；筛选选项标签与列表映射一致。

#### Scenario: 筛选列表不含废弃别名
- **WHEN** 用户展开运行日志「任务类型」筛选下拉
- **THEN** 选项仅包含当前实际任务类型的中文标签，不含已废弃的历史别名类型

### Requirement: 分页展示真实总条数

系统 SHALL 在运行日志页返回并展示符合当前筛选条件的真实总条数；分页页码、总条数 SHALL 与后端统计一致，不得通过「取超量行探测下一页」等方式估算。

#### Scenario: 总条数与后端一致
- **WHEN** 用户打开运行日志页或切换筛选条件
- **THEN** 分页组件「共 N 条」显示与当前筛选条件下记录总数一致的值

#### Scenario: 末页翻页不越界
- **WHEN** 用户翻到最后一页
- **THEN** 不再出现超出总条数范围的页码或空白页

### Requirement: 日志保留天数可动态配置

系统 SHALL 通过设置页配置运行日志（task_run）与容量快照的保留天数，配置值 SHALL 立即生效并持久化；历史清理任务 SHALL 按该配置删除早于保留期截止点的记录；未配置时 SHALL 使用默认值 30 天。

#### Scenario: 设置页配置保留天数
- **WHEN** 管理员在设置页把「运行日志保留天数」改为 N（如 60）
- **THEN** 配置保存成功且立即生效，后续历史清理任务按 N 天执行

#### Scenario: 默认保留天数
- **WHEN** 系统未配置运行日志保留天数
- **THEN** 历史清理按默认 30 天执行
