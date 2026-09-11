# Comet Spec Context

- Change: fix-online-issues
- Phase: design
- Mode: beta
- Context hash: 60272beda0048a5d9eed217864f66dc50da72ec773d6995c775a7e01d7526030

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/fix-online-issues/proposal.md
- SHA256: 842c4dc8ca5f3a02646978b72bae81d6f32bfe418aeea75c0cb9f30aff0232a6
- Source: docs/openspec/changes/fix-online-issues/design.md
- SHA256: 44b7372c8d341a219e953a6ca619e3ed18513f498dba43b855a8724e5c2b6793
- Source: docs/openspec/changes/fix-online-issues/tasks.md
- SHA256: cbc1be5247ad990988613e6da15cf690333dc958720755fdc26bba3882c4cc53
- Source: docs/openspec/changes/fix-online-issues/specs/emby-library-browse/spec.md
- SHA256: 6db3c5974d04e5f2a5550473d9963e2f4a9069a9e463412ca2cb96ba0a2cd9fa
- Source: docs/openspec/changes/fix-online-issues/specs/media-detail-ui/spec.md
- SHA256: 8bbb43f116b22c636d2bc7520aca496f67dca553ddf71abf2b4001f973aff45f
- Source: docs/openspec/changes/fix-online-issues/specs/media-status/spec.md
- SHA256: 6a6c032362db059ad445f5603a67100fdcfa44d0fa4e4c1633fcbb61392ae9ee
- Source: docs/openspec/changes/fix-online-issues/specs/queue-inspection-display/spec.md
- SHA256: 78e52d1c3d42612b7175f14a2fe20b38234ab530418dd379f26089a1d2e8e70a
- Source: docs/openspec/changes/fix-online-issues/specs/user-role-display/spec.md
- SHA256: 8606ec7c4643acebaf44f8915e9ba87f4be2db70bf5ccff1747faf9694ad9afc

## Acceptance Projection

## docs/openspec/changes/fix-online-issues/specs/emby-library-browse/spec.md

- Source: docs/openspec/changes/fix-online-issues/specs/emby-library-browse/spec.md
- Lines: 1-31
- SHA256: 6db3c5974d04e5f2a5550473d9963e2f4a9069a9e463412ca2cb96ba0a2cd9fa

```md
## MODIFIED Requirements

### Requirement: Emby 封面经代理加载

系统 SHALL 为 Emby 条目封面提供后端代理加载：后端生成的 Emby 封面代理地址 SHALL 能被代理路径校验通过（含 `/emby/<itemId>/Primary` 前缀与合法 itemId），前端不直连 Emby、不暴露 api_key；代理响应带缓存（进程内 TTL + 浏览器 Cache-Control）。

#### Scenario: 封面经代理显示
- **WHEN** Emby 条目存在封面
- **THEN** 前端通过代理地址加载封面并正常显示（后端构造的代理路径通过校验，不返回 400）

#### Scenario: 封面失败降级
- **WHEN** 代理拉取封面失败
- **THEN** 前端按既有海报缺失兜底逻辑显示占位，不破版

#### Scenario: 不暴露 api_key
- **WHEN** 前端请求 Emby 封面
- **THEN** 请求地址不携带 Emby api_key，安全鉴别由代理/服务端通道承载

## ADDED Requirements

### Requirement: 加入订阅仅管理员可见

Emby 影视库条目「加入订阅」按钮 SHALL 仅对管理员（admin）角色展示；非管理员（guest）用户浏览 Emby 影视库时不展示「加入订阅」/「匹配 TMDB 后订阅」等订阅入口，仅可浏览与查看。

#### Scenario: 管理员可见订阅入口
- **WHEN** 管理员查看 Emby 影视库中未收录条目
- **THEN** 条目展示「加入订阅」按钮，可发起订阅

#### Scenario: 访客不显示订阅入口
- **WHEN** 非管理员（guest）查看 Emby 影视库
- **THEN** 条目不展示「加入订阅」/「匹配 TMDB 后订阅」按钮及订阅相关入口，仅浏览与查看

```

## docs/openspec/changes/fix-online-issues/specs/media-detail-ui/spec.md

- Source: docs/openspec/changes/fix-online-issues/specs/media-detail-ui/spec.md
- Lines: 1-13
- SHA256: 8bbb43f116b22c636d2bc7520aca496f67dca553ddf71abf2b4001f973aff45f

```md
## MODIFIED Requirements

### Requirement: 状态下拉完整可见

影视详情页「状态」下拉选择器 SHALL 提供且仅提供「订阅中」（tracking）与「已暂停」（paused）两个选项；后端 media.status 字段 SHALL 仅接受这两个合法值，下载等执行态不得写入该字段；状态下拉选中后所选项文本完整可见，不被截断为仅显示箭头。

#### Scenario: 状态下拉完整显示选项
- **WHEN** 用户在详情页打开「状态」下拉
- **THEN** 下拉仅展示「订阅中」「已暂停」两个选项，且选中项文本完整显示，无截断

#### Scenario: 状态字段仅两值
- **WHEN** 影视详情接口返回 media.status
- **THEN** status 值仅可能为 tracking 或 paused；若历史数据存在其他值（如 download/downloading），系统以中文展示兜底（不原样透传英文），且状态仍按两值语义处理

```

## docs/openspec/changes/fix-online-issues/specs/media-status/spec.md

- Source: docs/openspec/changes/fix-online-issues/specs/media-status/spec.md
- Lines: 1-25
- SHA256: 6a6c032362db059ad445f5603a67100fdcfa44d0fa4e4c1633fcbb61392ae9ee

```md
## MODIFIED Requirements

### Requirement: 影视列表集数统计真实展示

系统 SHALL 在影视列表返回每部影视的集数统计（已有集数与缺失集数），其中「已有」按实际可观看维度聚合：Emby 实际入库集数 ∪ 本系统已下载完成集数（去重）；「缺失」= TMDB 全集数中已开播的集数 − 已有，未开播（TMDB 首播日期晚于今天）的集 SHALL 不计入缺失。「已有 x/xx 集」文案改为「已有 N 缺失 M」。不得因聚合逻辑缺失而恒为 0，不得把未开播集误计为缺失。

#### Scenario: 列表展示真实已入库集数
- **WHEN** 某剧集已有 5 集完成入库、全集 20 集
- **THEN** 列表显示「已有 5 缺失 15」，而非「已有 0/20 集」

#### Scenario: 已有包含 Emby 实际入库
- **WHEN** 某剧 1-3 集已在 Emby 库中（本系统下载/任务队列无记录）
- **THEN** 「已有」仍计入这 3 集（以 Emby 实际入库为准），缺失为 17

#### Scenario: 未开播集不计入缺失
- **WHEN** 某剧 TMDB 全集 24 集，其中已开播 12 集、尚未开播 12 集，已入库 8 集
- **THEN** 「已有」为 8、缺失为 4（12 已开播 − 8 已有），未开播的 12 集不计入缺失

#### Scenario: Emby 未配置回退
- **WHEN** Emby 未配置或该影视不在任何 Emby 库中
- **THEN** 「已有」回退为已下载完成集数，缺失为已开播集数减去该值，不报错

#### Scenario: 电影集数统计
- **WHEN** 影视类型为电影
- **THEN** 列表不显示集数统计，回退到电影状态展示

```

## docs/openspec/changes/fix-online-issues/specs/queue-inspection-display/spec.md

- Source: docs/openspec/changes/fix-online-issues/specs/queue-inspection-display/spec.md
- Lines: 1-13
- SHA256: 78e52d1c3d42612b7175f14a2fe20b38234ab530418dd379f26089a1d2e8e70a

```md
## ADDED Requirements

### Requirement: 访客队列页面不误报权限提示

非管理员（guest）用户访问任务队列/巡检队列页面时，系统 SHALL 允许浏览队列列表（只读）；控制类操作入口（暂停开关、取消/置顶/跳过/重试/排序、分享码等）按角色禁用或隐藏。页面加载不得因权限不足而向访客弹出「需要管理员权限」等错误提示；仅当访客实际触发被禁用的控制操作时才可提示权限不足。

#### Scenario: 访客浏览队列列表
- **WHEN** 非管理员（guest）打开任务队列页面
- **THEN** 队列列表正常展示，页面不弹出「需要管理员权限」等权限错误提示

#### Scenario: 访客操作入口禁用
- **WHEN** 非管理员（guest）查看任务队列页面中的控制类操作
- **THEN** 控制操作入口禁用或隐藏，访客不可触发（点击无响应或不可见），不产生权限错误弹窗

```

## docs/openspec/changes/fix-online-issues/specs/user-role-display/spec.md

- Source: docs/openspec/changes/fix-online-issues/specs/user-role-display/spec.md
- Lines: 1-17
- SHA256: 8606ec7c4643acebaf44f8915e9ba87f4be2db70bf5ccff1747faf9694ad9afc

```md
## Purpose

为用户管理提供角色信息的只读展示：管理员可在用户管理列表查看每个用户的角色（管理员/访客），角色仅作展示，不提供修改入口。

## ADDED Requirements

### Requirement: 用户角色只读展示

系统 SHALL 在用户管理列表展示每个用户的角色（admin 显示为「管理员」，guest 显示为「访客」）；角色 SHALL 仅作展示，界面不得提供修改角色的编辑控件（下拉/选择器）。

#### Scenario: 管理员查看用户角色
- **WHEN** 管理员打开用户管理列表
- **THEN** 每个用户行展示其角色标签（管理员/访客），无角色修改下拉或编辑入口

#### Scenario: 角色不可修改
- **WHEN** 管理员尝试在用户管理界面修改某用户角色
- **THEN** 界面不存在任何角色修改控件，角色字段仅可读展示

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.