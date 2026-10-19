# Comet Spec Context

- Change: media-min-size-and-emby-redownload
- Phase: design
- Mode: beta
- Context hash: f2652c417335c00b846bf9591ba15351ed57bf25049f938dbcdd822ccc020755

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/media-min-size-and-emby-redownload/proposal.md
- SHA256: c903cb2193ac53cfbc901c2dbdf8cdc03d898b8bde1f1ca05a7677ffffa29433
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/design.md
- SHA256: 490b55b74be27763d6f300113c9b4e4b3aa72d45e43d0b398b9c08f56497f2ed
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/tasks.md
- SHA256: 9b5577f1f462e6a86731258965153723a4ee2d9210fd9ae42ae35c75dfee893d
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/.openspec.yaml
- SHA256: b552ab6519df8f66b705724100f565c5cd842bc9389aeaa177da9cec3d02a685
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-detail-ui/spec.md
- SHA256: 4f8ae240b56d062ad45e4f2bdbce78f6b399aff08dcd275e4232aa95950973d7
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-pipeline/spec.md
- SHA256: 718da3600d744a3ca8782c67973a6e38112f1bc05db538afcb2a0509a522cfd5
- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-size-filter/spec.md
- SHA256: d31f9e867795315ba1b11eeef274f9349d35c3cc259c5495f5de2aef7de2bfbf

## Acceptance Projection

## docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-detail-ui/spec.md

- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-detail-ui/spec.md
- Lines: 1-19
- SHA256: 4f8ae240b56d062ad45e4f2bdbce78f6b399aff08dcd275e4232aa95950973d7

```md
# Spec Delta

## ADDED Requirements

### Requirement: 详情页配置影视大小上下限

影视详情页的大小设置 SHALL 同时提供单集与电影的最小大小、最大大小输入项，与既有控件并排展示。输入可为空（表示不限制）或非负数值；保存 SHALL 通过影视更新接口提交，保存成功后立即对后续巡检生效。

#### Scenario: 配置最小大小并保存生效
- **WHEN** 用户在影视详情页填写单集最小大小为 0.1 并保存
- **THEN** 该影视的单集最小大小被保存为 0.1，后续巡检按该值过滤资源文件

#### Scenario: 留空表示不限制
- **WHEN** 用户将某项大小输入清空并保存
- **THEN** 该维度保存为空，表示不限制，巡检不对该维度做大小过滤

#### Scenario: 拒绝负值输入
- **WHEN** 用户在大小输入项中尝试输入负数
- **THEN** 控件不接受该输入，不会提交无效的大小限制

```

## docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-pipeline/spec.md
- Lines: 1-27
- SHA256: 718da3600d744a3ca8782c67973a6e38112f1bc05db538afcb2a0509a522cfd5

```md
# Spec Delta

## ADDED Requirements

### Requirement: Emby 侧删除后自动重新下载

巡检 SHALL 识别「本系统已下载完成、但 Emby 中已不存在」的集，并在确认后解除该集的防重记录，使其重新进入搜索下载流程；确认前 SHALL 保留宽限轮次以吸收 Emby 扫描入库延迟。同一集因该原因触发的自动重新下载 SHALL 受次数上限约束：达到上限且 Emby 仍缺失时 SHALL 转为失败终态，保留人工重试入口，不得无限自动循环。

#### Scenario: 用户删除后自动重新下载
- **WHEN** 某集已下载完成，用户已在 Emby 删除该条目，且宽限轮次已用尽
- **THEN** 该集防重记录被解除，后续巡检重新搜索并入队下载该集

#### Scenario: 宽限期内不重复下载
- **WHEN** 某集刚下载完成、Emby 尚未完成扫描，缺失判定中仍包含该集
- **THEN** 在宽限轮次内不解除防重、不重复下载该集，等待 Emby 确认

#### Scenario: 超过自动重下次数上限
- **WHEN** 同一集因 Emby 缺失触发的自动重新下载已达次数上限，且 Emby 中仍缺失该集
- **THEN** 该集转为失败终态，不再自动重新下载，用户仍可通过队列页手动重试

#### Scenario: 入库确认后解除防重保留
- **WHEN** 某集已下载完成且 Emby 已确认收录
- **THEN** 该集防重记录被解除，不产生重复下载

#### Scenario: 失败记录不再永久阻挡重新下载
- **WHEN** 某集存在因「下载完成但 Emby 未入库」产生的失败记录，且该集在 Emby 中确实缺失
- **THEN** 巡检不再把该记录视为不可恢复的终态，按自动重新下载规则处理该集

```

## docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-size-filter/spec.md

- Source: docs/openspec/changes/media-min-size-and-emby-redownload/specs/media-size-filter/spec.md
- Lines: 1-51
- SHA256: d31f9e867795315ba1b11eeef274f9349d35c3cc259c5495f5de2aef7de2bfbf

```md
# Spec Delta

## Purpose

为影视资源下载提供大小策略：按单集/电影分设大小上限与下限，支持全局默认与单影视覆盖，避免下载过小（如 AI 生成的短片）或过大的错误资源占用空间与配额。

## ADDED Requirements

### Requirement: 影视大小限制配置口径

系统 SHALL 为单集与电影分别提供「最大大小」与「最小大小」限制，每项均支持全局默认值与单影视覆盖值，生效优先级为「单影视覆盖 > 全局配置 > 代码默认」。配置值留空或为 0 时 SHALL 表示该维度不限制。全局值 SHALL 可在系统设置页配置，单影视值 SHALL 可在该影视详情页配置，保存后立即对后续巡检生效。

#### Scenario: 使用全局默认值
- **WHEN** 某影视未设置单影视大小限制，且全局配置了单集大小下限
- **THEN** 该影视的巡检使用全局下限值过滤资源文件

#### Scenario: 单影视覆盖全局值
- **WHEN** 全局配置单集大小下限为 A，某影视单独配置为 B
- **THEN** 该影视使用 B 过滤资源文件，其他影视仍使用 A

#### Scenario: 留空或 0 表示不限制
- **WHEN** 某项大小限制留空或配置为 0
- **THEN** 该维度不做任何大小过滤，任意大小的文件均可入队

#### Scenario: 电影与剧集使用各自的阈值
- **WHEN** 同一轮巡检同时处理电影与剧集
- **THEN** 电影按电影阈值过滤，剧集按单集阈值过滤，互不混用

### Requirement: 巡检入队前按大小过滤资源文件

巡检探测匹配到具体资源文件后，系统 SHALL 在写入任务队列前按该影视生效的上下限过滤：文件大小低于下限或高于上限时 SHALL 跳过该文件、不写入任务队列，并计入巡检结果的大小过滤跳过计数。文件大小未知时 SHALL 维持现有「宁缺勿滥」策略跳过该文件。过滤 SHALL NOT 中断本轮巡检的其余文件处理。

#### Scenario: 过小文件被下限拦截
- **WHEN** 某剧集单集资源文件大小约为 20MB，而生效的下限为 0.1GB
- **THEN** 该文件被跳过，不写入任务队列，巡检结果记录 1 个大小过滤跳过

#### Scenario: 过大文件仍被上限拦截
- **WHEN** 资源文件大小超过生效上限
- **THEN** 该文件被跳过，不写入任务队列，巡检结果记录 1 个大小过滤跳过

#### Scenario: 边界值放行
- **WHEN** 资源文件大小恰好等于下限或上限
- **THEN** 该文件不被大小过滤拦截，正常进入入队流程

#### Scenario: 大小未知保守跳过
- **WHEN** 探测结果无法得到资源文件大小
- **THEN** 该文件被跳过，不写入任务队列，行为与现有未知大小策略一致

#### Scenario: 过滤不中断本轮巡检
- **WHEN** 同一轮巡检中存在多个被大小过滤拦截的文件，同时存在符合大小要求的文件
- **THEN** 被拦截文件仅被跳过，符合要求的文件仍正常入队

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.
Task hash policy: task-content-v1. Read tasks.md for live completion; task hashes ignore checkbox completion only.