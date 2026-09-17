# Comet Spec Context

- Change: fix-logs-and-ui-polish
- Phase: design
- Mode: beta
- Context hash: ce0439203b5be2bb6fda62b7a341024ed670b39c68c7f85fbf4d126e49260bdb

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/fix-logs-and-ui-polish/proposal.md
- SHA256: c1656f94c4dd44db25384d4843745c90757649c29864070ff50ccfed75f063ab
- Source: docs/openspec/changes/fix-logs-and-ui-polish/design.md
- SHA256: 8c6792bf75a078f0ba09be57f24b653b4412ebb6d8e75807f342fbf4fa48578b
- Source: docs/openspec/changes/fix-logs-and-ui-polish/tasks.md
- SHA256: 28a0bf4760766eabe2faccb480832e053e1fa14d751f58e499878d21d0b3ddac
- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/media-status/spec.md
- SHA256: b8893eacb4a0e94ba0817f3cb7bf34bbb9fcae3300470f4ea9502ebb9efc6a30
- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/run-logs/spec.md
- SHA256: 56e0225a454a34f3b60d0b770c9d002855a98b1b85dd20c76c453909cd5113ee
- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/user-invite-management/spec.md
- SHA256: dc9b2cbd49441a8940fb992f2ef77aee456b41d08d73bf8996685be2d61f6bb2

## Acceptance Projection

## docs/openspec/changes/fix-logs-and-ui-polish/specs/media-status/spec.md

- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/media-status/spec.md
- Lines: 1-16
- SHA256: b8893eacb4a0e94ba0817f3cb7bf34bbb9fcae3300470f4ea9502ebb9efc6a30

```md
## ADDED Requirements

### Requirement: 集数统计文案唯一前缀

影视列表卡片集数统计 SHALL 以单个「已有」前缀渲染「已有 N 缺失 M」文案，不得因模板前缀与统计函数返回值各自携带「已有」而重复渲染为「已有 已有 N 缺失 M」；电影等无集数统计场景 SHALL 不显示该前缀。

#### Scenario: 卡片集数统计不重复前缀
- **WHEN** 某剧集已有 15 集、缺失 0 集，影视库以卡片模式展示
- **THEN** 卡片显示「已有 15 缺失 0」，而非「已有 已有 15 缺失 0」

#### Scenario: 缺失大于 0 时的文案
- **WHEN** 某剧集已有 5 集、缺失 15 集
- **THEN** 卡片显示「已有 5 缺失 15」，前缀仅出现一次

#### Scenario: 电影不显示集数统计
- **WHEN** 影视类型为电影且以卡片模式展示
- **THEN** 卡片不显示「已有」集数统计文案，回退到电影状态展示
```

## docs/openspec/changes/fix-logs-and-ui-polish/specs/run-logs/spec.md

- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/run-logs/spec.md
- Lines: 1-48
- SHA256: 56e0225a454a34f3b60d0b770c9d002855a98b1b85dd20c76c453909cd5113ee

```md
## Purpose

运行日志页面提供任务执行记录的可读展示与生命周期管理：任务类型中文映射、基于后端真实 total 的分页，以及日志保留天数动态配置。

## ADDED Requirements

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
```

## docs/openspec/changes/fix-logs-and-ui-polish/specs/user-invite-management/spec.md

- Source: docs/openspec/changes/fix-logs-and-ui-polish/specs/user-invite-management/spec.md
- Lines: 1-40
- SHA256: dc9b2cbd49441a8940fb992f2ef77aee456b41d08d73bf8996685be2d61f6bb2

```md
## Purpose

用户管理页承载邀请码（分享码）的完整管理能力：生成、复制、复制注册链接与删除，作为用户注册准入管理的一部分，与用户列表同页操作。

## ADDED Requirements

### Requirement: 邀请码管理位于用户管理页

系统 SHALL 将邀请码（分享码）管理（生成、复制、复制注册链接、删除）从设置页迁移至用户管理页展示，设置页 SHALL 不再包含「邀请码管理」入口；邀请码数据与后端 API 保持既有契约不变。

#### Scenario: 用户管理页展示邀请码管理
- **WHEN** 管理员打开用户管理页
- **THEN** 页面提供邀请码管理区块，可生成、复制、复制注册链接与删除邀请码

#### Scenario: 设置页不再含邀请码管理
- **WHEN** 管理员打开设置页
- **THEN** 页面不再展示「邀请码管理」tab 或入口

### Requirement: 邀请码管理操作可用

用户管理页中的邀请码管理 SHALL 支持：生成指定数量邀请码、复制邀请码文本、复制注册链接（站点地址 + code 参数）、删除未使用邀请码（已使用邀请码删除返回明确提示）；操作结果 SHALL 有成功/失败反馈。

#### Scenario: 生成邀请码
- **WHEN** 管理员在用户管理页指定数量并点击「生成邀请码」
- **THEN** 生成对应数量的可用邀请码并展示在列表顶部，页面给出成功提示

#### Scenario: 复制邀请码
- **WHEN** 管理员点击某邀请码的「复制」
- **THEN** 邀请码文本复制到剪贴板，页面提示「已复制邀请码」

#### Scenario: 复制注册链接
- **WHEN** 管理员点击某邀请码的「复制注册链接」
- **THEN** 形如 `{站点}/register?code={邀请码}` 的链接复制到剪贴板，页面提示「已复制注册链接」

#### Scenario: 删除未使用邀请码
- **WHEN** 管理员点击某未使用邀请码的「删除」并确认
- **THEN** 该邀请码被删除，列表刷新

#### Scenario: 删除已使用邀请码受限
- **WHEN** 管理员尝试删除已被使用的邀请码
- **THEN** 删除按钮不可用或后端返回明确错误提示，该邀请码保留
```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.