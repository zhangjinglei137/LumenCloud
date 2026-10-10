# Comet Spec Context

- Change: gate-missing-episodes-by-airdate
- Phase: design
- Mode: beta
- Context hash: 8414fbd790aae66fcde1a6d540cceefb96eb8794587dec1b6f0c2d55d78b8f2d

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This beta context pack verbatim-projects spec files and references supporting artifacts by hash, not an agent-authored summary.

## Source References

- Source: docs/openspec/changes/gate-missing-episodes-by-airdate/proposal.md
- SHA256: 53ee07a46d1b8583d3935ed07fbb1bedc4c12546a92ea0b6b32e823474381544
- Source: docs/openspec/changes/gate-missing-episodes-by-airdate/design.md
- SHA256: cc6313e7e5233d0669ca6d8b8fea5aa75b1c932db0cde0c90ef86bc47b4ce90e
- Source: docs/openspec/changes/gate-missing-episodes-by-airdate/tasks.md
- SHA256: 10f3c8ecafc458d721c22efee57a2e473fe9bdc8d6f4ba8ffeaefc56aec511cd
- Source: docs/openspec/changes/gate-missing-episodes-by-airdate/specs/media-pipeline/spec.md
- SHA256: a65e0b1cef41294e8ab9518dca7327a556402950d5ce223a923ca6a04c0aa5f3

## Acceptance Projection

## docs/openspec/changes/gate-missing-episodes-by-airdate/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/gate-missing-episodes-by-airdate/specs/media-pipeline/spec.md
- Lines: 1-45
- SHA256: a65e0b1cef41294e8ab9518dca7327a556402950d5ce223a923ca6a04c0aa5f3

```md
## MODIFIED Requirements

### Requirement: 巡检产出缺失集任务落任务队列

巡检判定「缺失集」时 SHALL 应用「已播出」门控：仅当某集**播出日期已知且当前日期不早于该播出日期**（即已播出）、且**媒体库中不存在**时，才视为缺失集并进入搜索下载流程。**播出日期缺失或无法解析的集 SHALL 视为未播出，不计入缺失集、不搜索、不入队**。播出日期 SHALL 以 TMDB 首播日期（`air_date`）为准。该门控 SHALL 同时作用于常规巡检（Emby 遗漏集基线）与全量兜底路径（Emby 未收录该剧时的全量模式）。

巡检发现（已播出之）缺失集（剧集 SxxExx）或缺失影视（电影）时，SHALL 使用标题别名集合构造多关键词搜索，将经季号校验与候选评分后确认匹配的缺失集信息（含转存所需信息：分享码、stoken、fids、fid_tokens、folder_id 等）写入任务队列；搜索并收集完资料后，该影视该轮巡检即视为完成，巡检侧不再继续执行转存/下载。季号不符或匹配证实的错配候选 SHALL 被拒绝入队。

当播出日期回源失败（上游不可用）时，系统 SHALL NOT 因数据缺失而将全部集判为未播出并整体停搜；SHALL 采取不误停的降级策略并记录，不得把「查询失败」与「该集无播出日期」混为一谈。

#### Scenario: 巡检发现缺失集并搜集资料
- **WHEN** 巡检搜索到某影视的已播出缺失集且成功收集分享信息
- **THEN** 任务队列新增一条该影视缺失集任务，携带完整转存所需信息，巡检任务标记完成

#### Scenario: 缺失集搜索无结果
- **WHEN** 巡检对某缺失集搜索后未找到可用分享资源
- **THEN** 该缺失集不入任务队列，并在巡检结果中记录未找到源

#### Scenario: 错季候选不入队
- **WHEN** 目标为 S02 而候选标题季号为 S01（如「斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29」）
- **THEN** 候选被季号校验拒绝，不入任务队列，继续搜索其它候选

#### Scenario: 无播出日期的未开播集不搜索下载
- **WHEN** 某集在媒体库中不存在、且在播出日期数据源中查询不到播出日期（如「剑来 S03E01」尚未公布首播日期）
- **THEN** 该集被视为未播出，不计入缺失集，本轮不构造关键词、不搜索、不入队

#### Scenario: 首播日期在未来不搜索下载
- **WHEN** 某集在媒体库中不存在，且其播出日期晚于当前日期
- **THEN** 该集被视为未播出，不计入缺失集，不搜索、不入队

#### Scenario: 已播出的缺失集正常搜索
- **WHEN** 某集在媒体库中不存在，且其播出日期已知且不晚于当前日期
- **THEN** 该集被视为缺失集，进入关键词构造与搜索入队流程

#### Scenario: 同剧部分已播、部分未播仅已播集入队
- **WHEN** 同一剧既有已播出（日期 ≤ 今天）又有未播出（日期在未来或无日期）的缺失集
- **THEN** 仅已播出集被判定为缺失并进入搜索入队，未播出集被排除，二者互不影响

#### Scenario: 无日期的未开播剧在全量兜底路径同样被拦截
- **WHEN** 某剧尚未在 Emby 收录、进入全量兜底路径，且其集数无播出日期或播出日期在未来
- **THEN** 该剧相关集不因全量模式被整部误搜，不产生搜索下载任务

#### Scenario: 播出日期回源失败不整体停搜
- **WHEN** 播出日期数据源查询失败（上游不可用/超时）
- **THEN** 系统不将全部集判为未播出而整体停搜，采取不误停的降级策略并记录该失败，下一轮恢复后按正常门控判定

```

Full source files remain canonical. If a required heading or scenario is missing here, regenerate the handoff or read the source spec directly. Supporting files (proposal, design, tasks) are referenced by hash only.