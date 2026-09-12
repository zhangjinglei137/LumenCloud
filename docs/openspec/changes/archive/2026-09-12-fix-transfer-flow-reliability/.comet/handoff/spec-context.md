# Comet Spec Context

- Change: fix-transfer-flow-reliability
- Phase: design
- Mode: beta
- Context hash: d762c091e44a277a3435316661ef9bd43a6b8e8e7d9684fd94e827596ff2224e

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/fix-transfer-flow-reliability/proposal.md
- SHA256: 5ad3f82e08c22874f0d96fb6774001e8358e54bdd831de759de72f38050a6277
- Source: docs/openspec/changes/fix-transfer-flow-reliability/design.md
- SHA256: 7f2bad87f8face6745aaf2c1b23ad058341f042b5f3a1e0edcb7147b40161125
- Source: docs/openspec/changes/fix-transfer-flow-reliability/tasks.md
- SHA256: 1d0f027b3ced86854f52f227a7a819a1710f8c15d2fefe5e60460ce8705def2e
- Source: docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-admission/spec.md
- SHA256: 8c4edfee91333cf0fe0cee2d64587e712bf7ae7df0c12a3bbe5fc191899f9579
- Source: docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-transfer/spec.md
- SHA256: 929d52aba686af2d905f1a68e9242d503f41a0d09359b579fe50c4a4f60598ca

## Acceptance Projection

## docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-admission/spec.md

- Source: docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-admission/spec.md
- Lines: 1-53
- SHA256: 8c4edfee91333cf0fe0cee2d64587e712bf7ae7df0c12a3bbe5fc191899f9579

```md
# pipeline-admission Delta Specification

## MODIFIED Requirements

### Requirement: 容量判断准入

下载队列 SHALL 以「任务文件大小 + 在途下载文件合计 ≤ 网盘最大容量」作为开始下载的唯一准入条件；准入通过后任务进入转存下载流程。容量判断 SHALL 不得在持有数据库写入事务期间执行网络 IO（如递归统计网盘目录），且 SHALL 将已落盘但仍在下载中的文件计入占用，避免容量记账漏计导致突破网盘容量上限。

#### Scenario: 容量充足直接下载
- **WHEN** 下载队列取到任务且（任务大小 + 在途下载合计）小于等于网盘容量
- **THEN** 该任务进入转存下载流程

#### Scenario: 容量不足进入等待
- **WHEN** 下载队列取到任务且（任务大小 + 在途下载合计）大于网盘容量
- **THEN** 该任务进入等待状态，不开始下载，按入队顺序排队

#### Scenario: 准入判定不阻塞数据库写入
- **WHEN** 准入流程执行容量判断且容量数据需实时统计
- **THEN** 容量统计在数据库事务外完成，且不因持有写锁阻塞其他数据库操作

#### Scenario: 已落盘下载文件计入容量
- **WHEN** 某文件已转存落盘但仍在 downloading 状态，同时用于容量准入的用量缓存尚未刷新
- **THEN** 该文件仍被计入已用容量，不得因缓存滞后导致超容量准入

### Requirement: 等待队列排队与唤醒

容量不足的任务 SHALL 在等待队列按序排队；当任一在途任务完成释放容量后，系统 SHALL 重新评估等待队列首位任务，容量满足则续跑下载。等待队列的唤醒与回置 SHALL 避免无谓的全量写放大与外部容量查询风暴。

#### Scenario: 下载完成释放容量后续跑
- **WHEN** 某下载任务完成并释放容量预留
- **THEN** 系统重新按序评估等待队列，容量满足的任务进入下载流程

#### Scenario: 多个任务排队依序推进
- **WHEN** 连续释放容量
- **THEN** 等待队列按入队顺序逐个推进，直至容量再次不足

#### Scenario: 等待积压时唤醒有界
- **WHEN** 等待队列存在大量积压任务且容量仍不足
- **THEN** 系统避免每轮对全部等待任务执行唤醒-回置与重复容量统计，控制写放大与外部调用量

## ADDED Requirements

### Requirement: 转存来源校验与逃生

系统在准入转存前 SHALL 校验 aria2 活动/等待任务的来源，仅当任务 gid 属于本系统已签发集合时才继续转存；发现非本系统任务 SHALL 暂停本批转存并告警。该校验 SHALL 提供逃生通道，避免孤儿任务（清理失败的残留 gid）永久阻断后续转存。

#### Scenario: 陌生任务拦截
- **WHEN** aria2 中存在 gid 不在本系统已签发集合的活动/等待任务
- **THEN** 本轮转存暂停并告警，下轮自动重试

#### Scenario: 孤儿任务不永久自锁
- **WHEN** 同一陌生 gid 持续存在且清理动作一直失败
- **THEN** 系统在连续跳过达到阈值后自动执行一次清理尝试，或放宽白名单口径，避免转存流程永久停摆

```

## docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-transfer/spec.md

- Source: docs/openspec/changes/fix-transfer-flow-reliability/specs/pipeline-transfer/spec.md
- Lines: 1-69
- SHA256: 929d52aba686af2d905f1a68e9242d503f41a0d09359b579fe50c4a4f60598ca

```md
# pipeline-transfer Delta Specification

## MODIFIED Requirements

### Requirement: 下载完成进入转移

下载任务完成后，系统 SHALL 通过 NasTools 完成转移整理；以 nastools Webhook（transfer.finished）或其他等价信号判断转移是否完成。Webhook 推进 SHALL 按文件级（文件名/集号）匹配推进单个任务，不得按 media 批量推进未完成转移的任务；推进时 SHALL 重置该任务的节点重试计数与诊断字段。

#### Scenario: 判定转移完成
- **WHEN** 下载任务完成且收到 NasTools 转移完成 Webhook
- **THEN** 系统推进该任务进入入库确认阶段

#### Scenario: Webhook 仅推进已完成的单文件任务
- **WHEN** 某影视存在多个任务处于转移阶段且收到其中单文件的 transfer.finished 事件
- **THEN** 系统仅推进与该文件匹配的单个任务，其余任务保持转移阶段等待各自完成信号

#### Scenario: Webhook 无法定位文件时兜底轮询
- **WHEN** Webhook 载荷无法定位到具体任务文件
- **THEN** 系统不批量推进，仅触发入库确认轮询加速，由轮询按既有规则判定

#### Scenario: 推进时重置节点状态
- **WHEN** 任务由转移阶段推进到入库确认阶段
- **THEN** 系统重置该任务在转移阶段的失败计数与诊断字段，避免旧计数影响后续节点判定

## ADDED Requirements

### Requirement: 转存提交冲突时清理夸克残留

转存链在提交下载任务时若发生并发状态冲突（任务已被回退），系统 SHALL 在清理已提交的 aria2 任务之外，同时 best-effort 清理已转存到夸克中转目录的文件，避免夸克空间累积泄漏导致容量假性不足。

#### Scenario: 提交冲突清理中转文件
- **WHEN** 转存成功但提交下载状态时 CAS 冲突失败
- **THEN** 系统尽力删除已转存的夸克中转文件并清理 aria2 任务，失败仅告警不阻断

### Requirement: 取件与任务创建原子化

系统从任务队列取件生成下载任务时 SHALL 保证取件与任务创建原子一致：因唯一键冲突导致任务创建失败时，取件标记不得残留为已完成，后续 SHALL 可再次取件或安全终止，不得出现「源任务标记完成但无下载任务」的丢失状态。

#### Scenario: 取件冲突不丢失任务
- **WHEN** 取件后创建下载任务撞唯一键冲突（如并发人工入队先建）
- **THEN** 源任务队列行不被误标完成，该影视缺失集仍可被后续巡检重新入队或由并发路径继续

### Requirement: 任务取消与跳过同步媒体状态

用户取消或跳过下载任务后，系统 SHALL 同步更新所属影视的下载状态；当该影视已无任何在途任务时，SHALL 将影视状态回退为可继续巡检状态，不得因残留 downloading 状态导致后续缺失集永久不再入队。

#### Scenario: 取消最后一个任务后媒体状态回退
- **WHEN** 用户取消某影视最后一个在途下载任务
- **THEN** 该影视状态回退，后续巡检发现的新缺失集可正常入队

### Requirement: 刮削失败有界重试

刮削（转移整理）执行失败时，系统 SHALL 对失败进行有界重试；外部服务持续不可用期间 SHALL 采用退避策略控制重试频率，不得以高频节拍对全部在刮削任务批量累加失败计数导致任务被集中误杀。

#### Scenario: 外部服务故障时退避重试
- **WHEN** NasTools 服务持续不可用导致刮削失败
- **THEN** 系统按退避策略降低重试频率，且不因外部故障对全部在刮削任务同步累加失败计数至失败终态

### Requirement: 入库确认的遗漏集复核

系统在入库确认阶段 SHALL 谨慎处理 Emby 遗漏集信息：当遗漏集列表为空或无法解析当前集号时，SHALL 延迟一轮复核后再判定已收录，不得立即视为收录并删除夸克中转文件，避免刮削滞后导致的误删。

#### Scenario: 遗漏集为空时延迟复核
- **WHEN** Emby 返回的遗漏集列表为空或无法匹配当前集号
- **THEN** 系统延迟一轮复核窗口后再判定已收录，不立即删除夸克中转文件

#### Scenario: 复核窗口内收录确认
- **WHEN** 复核窗口结束后 Emby 已确认该集收录
- **THEN** 系统正常标记完成并删除夸克中转文件

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.