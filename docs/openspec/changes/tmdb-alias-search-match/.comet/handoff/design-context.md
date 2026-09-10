# Comet Design Handoff

- Change: tmdb-alias-search-match
- Phase: design
- Mode: compact
- Context hash: ae58116439ad1455ab55fcf33987f35943c3f7d25a6f1154e400c204ab884f0e

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/tmdb-alias-search-match/proposal.md

- Source: docs/openspec/changes/tmdb-alias-search-match/proposal.md
- Lines: 1-29
- SHA256: 7b34c8b15c3da7a3d078136d4baf76dcf393fb99eace8a71e2437e1410feaa7c

```md
# Proposal: tmdb-alias-search-match

## Why

巡检搜索对中英文别名混搜场景匹配错误：搜索「斗罗大陆」时把「斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29.…」误收为候选（标题含「斗罗大陆」子串），而正确的英文名资源「Soul.Land.S02E167.…」因不含中文关键词被严格子串匹配直接剔除。根因是搜索链路只读 TMDB 中文主标题、无别名（also_known_as/original_title）扩展、无季号校验。

## What Changes

- **TMDB 别名扩展**：搜索/缓存时读取并存储 TMDB `also_known_as`、`original_title`（中英文多语言别名），为影视构建别名集合（含 `title → 别名` 映射）
- **多语言关键词构造**：巡检关键词由单一 `media.title + Sxx` 扩展为多关键词（中文主标题 / 英文原名 / 别名），云盘搜索按多关键词并行搜索后合并候选
- **候选匹配重构**：
  - 季号严格校验：候选集号 Sxx 必须与目标季号匹配，错季资源（如目标 S02 却命中 S01E29）直接拒绝
  - 匹配评分升级：别名/英文名匹配进入评分体系（原名命中、别名命中、年份校验），替代「标题子串放行」的宽松规则
  - 错误样例回归：`Soul.Land.S02E167` 命中；`The.Peerless.Tang.Clan.S01E29`（错部/错季）被拒
- 保持巡检队列任务语义与现有任务流转不变

## Capabilities

### New Capabilities
- `title-alias-search`: 标题别名扩展搜索与季号校验匹配（TMDB 别名数据源、多关键词并行搜索、候选评分与过滤）

### Modified Capabilities
- `media-pipeline`: 巡检产出缺失集任务（搜索关键词构造与候选资源筛选行为）

## Impact

- 后端：`backend/app/services/tmdb.py`（别名读取与缓存）、`backend/app/tasks/scan.py`（`_build_keywords` / `_rank_candidates` / `_share_title_relevant` 重构）、`backend/app/services/cloudsaver.py`（多关键词并行搜索）、数据模型（别名持久化，如需）
- 前端：无直接改动（搜索为后端链路）
- 依赖：TMDB API（already_known_as/original_title 字段）；可能新增轻量数据库表/列

```

## docs/openspec/changes/tmdb-alias-search-match/design.md

- Source: docs/openspec/changes/tmdb-alias-search-match/design.md
- Lines: 1-48
- SHA256: edf492340dd3bd175eef916ff96213299b7965c5f758e22c539ec35ff4cfca58

```md
## Context

搜索链路现状：`search_multi`（backend/app/services/tmdb.py:289-360）只读 `language=zh-CN` 主标题并 upsert `tmdb_cache`；`scan.py:_build_keywords`(693-710) 用 `media.title + Sxx` 构造单组关键词；`_share_title_relevant`(807-825) 用「完全相等 / 标题开头 / 标题子串」宽松放行；`_rank_candidates`(716-737) 权重为 title-in-name +10 / word-in-name +2 / year-in-name +5。错误样例：目标「斗罗大陆 S02」时，`斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29`（标题含「斗罗大陆」子串 + S01）被放行，而正确英文名 `Soul.Land.S02E167` 因不含中文关键词被剔除。TMDB 数据源有 `also_known_as`/`original_title` 字段但当前未读取。见 proposal.md - Why。

## Goals / Non-Goals

**Goals:**
- 为影视构建含中英文的多语言别名集合并持久化，作为关键词与匹配依据
- 巡检搜索从单关键词改为多关键词并行检索
- 引入季号校验，拒绝错季/错配候选；评分支持别名/原名成员匹配

**Non-Goals:**
- 不引入 AI/LLM 挑选（保持确定性规则）
- 不改下载队列消费与任务流转
- 不处理 TMDB 搜索页人工挑选交互（TmdbSearch 组件不动）

## Decisions

**D1: 别名集合持久化进 tmdb_cache（或新增轻量表列）**
- 理由：巡检按固定间隔执行，不能每次实时调 TMDB；别名应在影视入库存入时可一次拉取保存
- 实现：在 `search_multi`/详情同步时读取 `also_known_as`+`original_title`，归一化后存入影视记录（JSON 列或独立表）
- 备选：运行时实时调 TMDB 拉别名（慢、依赖外部）→ 放弃

**D2: 关键词构造多词并行，搜索去重合并**
- `_build_keywords` 输出 主标题 + 各别名 + 「标题 Sxx」+「别名 Sxx」候选集，cloudSaver 搜索改造为可并行多组查询，结果按 share 去重合并
- 避免关键词爆炸：别名归一化（小写、去标点、去年份/分辨率后缀），并限制最多 N 组（保留主标题 + 前 N 个高价值别名）

**D3: 匹配模型从「宽松子串」改为「成员令牌 + 季号校验」**
- 标题解析为令牌集合（normalize：小写、去符号、分片单词），候选与别名集合做交集命中 → 得匹配种类分
- 季号校验：目标缺失集 Sxx 与候选标题季号必须一致；S01 ≠ S02 → 拒绝
- 保留 year-in-name 加权；题目中「斗罗大陆Ⅱ」「绝世唐门」这类续作标题不再仅凭主标题子串放行

**D4: 拒绝规则优先级**
先季号校验（硬拒绝）→ 再标题成员匹配（软筛选）→ 评分排序取最高，全部规则确定性、可测试

## Risks / Trade-offs

- [别名集合膨胀致搜索变慢] → 别名数量上限 + 并行搜索 + 进程内结果合并，单轮巡检结果可复用
- [季号校验误杀正版资源] → 季号解析容错：标题无季号时降级到标题匹配（仅当目标季为 S01 或整体剧集打包资源除外）
- [英文别名与共享资源命名差异大] → 成员/前缀令牌匹配 + 年份加权缓解

## Migration Plan

- 别名持久化为增量（新字段，旧记录首次巡检时回填）
- 候选筛选逻辑单点替换，巡检任务落队行为保持一致

## Open Questions

无。
```

## docs/openspec/changes/tmdb-alias-search-match/tasks.md

- Source: docs/openspec/changes/tmdb-alias-search-match/tasks.md
- Lines: 1-22
- SHA256: 49e001eca1df7fed0af16c7e7a6519d15c77b03aedad8734e2351a9a5ed7d0a8

```md
# Tasks: tmdb-alias-search-match

## 1. TMDB 别名数据源

- [ ] 1.1 services/tmdb.py 读取 also_known_as/original_title（search_multi 与详情同步路径），归一化（小写/去标点/去后缀）后保存别名集合到 tmdb_cache（新增列或 JSON 扩展），验证已有影视记录含别名、缺失时回退主标题
- [ ] 1.2 为既有影视补充别名回填路径（巡检首轮或后台任务触发），验证存量记录可获得别名
- [ ] 1.3 补充测试：别名解析保存、别名缺失回退、归一化边界，验证 `pytest` 通过

## 2. 多关键词并行搜索

- [ ] 2.1 tasks/scan.py `_build_keywords` 重构：由 [{title} Sxx] 扩展为主标题/别名/原名的多关键词候选（限数量防膨胀），验证关键词集合覆盖中英文
- [ ] 2.2 services/cloudsaver.py search 支持多关键词并行检索并合并去重，验证各关键词结果合并、单组无结果不阻塞
- [ ] 2.3 补充测试：关键词构造、并行合并、季号后缀继承（别名 + Sxx），验证 `pytest` 通过

## 3. 候选匹配与季号校验

- [ ] 3.1 tasks/scan.py 新增季号解析与硬校验：目标季号 vs 候选标题季号（S01≠S02 拒绝；标题无季号时按目标 S01/整季资源降级），验证错季候选（The.Peerless.Tang.Clan.S01E29）被拒
- [ ] 3.2 `_share_title_relevant`/`_rank_candidates` 重构：标题令牌群与别名集合作成员/前缀匹配评分（原名命中、别名命中、年份加权），替代宽松子串放行，验证 Soul.Land.S02E167 入选并优先
- [ ] 3.3 回归测试：新增「斗罗大陆」场景用例（错部/错季拒绝、英文名命中）+「star」旧用例回归，验证 `pytest` 通过且既有行为不退化

## 4. 集成验证

- [ ] 4.1 真实环境（或模拟 cloudSaver）端到端：对「斗罗大陆」触发巡检，确认续作错季资源不落任务、Soul.Land 正确候选入队；`npm run build` 通过
```

## docs/openspec/changes/tmdb-alias-search-match/specs/media-pipeline/spec.md

- Source: docs/openspec/changes/tmdb-alias-search-match/specs/media-pipeline/spec.md
- Lines: 1-18
- SHA256: 0e13f4aa6f3a263fdf0fa323a2b3c2f0d1185301a344ea011858a2b36d0f88f1

```md
# media-pipeline Delta Spec

## MODIFIED Requirements

### Requirement: 巡检产出缺失集任务落任务队列

巡检发现缺失集（剧集 SxxExx）或缺失影视（电影）时，SHALL 使用标题别名集合构造多关键词搜索，将经季号校验与候选评分后确认匹配的缺失集信息（含转存所需信息：分享码、stoken、fids、fid_tokens、folder_id 等）写入任务队列；搜索并收集完资料后，该影视该轮巡检即视为完成，巡检侧不再继续执行转存/下载。季号不符或匹配证实的错配候选 SHALL 被拒绝入队。

#### Scenario: 巡检发现缺失集并搜集资料
- **WHEN** 巡检搜索到某影视的缺失集且成功收集分享信息
- **THEN** 任务队列新增一条该影视缺失集任务，携带完整转存所需信息，巡检任务标记完成

#### Scenario: 缺失集搜索无结果
- **WHEN** 巡检对某缺失集搜索后未找到可用分享资源
- **THEN** 该缺失集不入任务队列，并在巡检结果中记录未找到源

#### Scenario: 错季候选不入队
- **WHEN** 目标为 S02 而候选标题季号为 S01（如「斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29」）
- **THEN** 候选被季号校验拒绝，不入任务队列，继续搜索其它候选
```

## docs/openspec/changes/tmdb-alias-search-match/specs/title-alias-search/spec.md

- Source: docs/openspec/changes/tmdb-alias-search-match/specs/title-alias-search/spec.md
- Lines: 1-54
- SHA256: 73557f047bd67f16fa8d0a7851cdfc8fbd9febcbbd51514e74e5fb3a3f7bf015

```md
# title-alias-search Specification

## Purpose

为影视/剧集搜索提供基于 TMDB 多语言别名的扩展搜索与季号校验匹配：用别名构造多组云盘搜索关键词并行检索，按季号与评分精筛候选资源，减少中英文混搜误收与漏收。

## ADDED Requirements

### Requirement: TMDB 别名扩展

系统 SHALL 在搜索与缓存影视元数据时读取并保存 TMDB 的 `also_known_as`、`original_title` 等别名字段，形成影视的多语言别名集合；别名缺失时回退主标题。

#### Scenario: 别名集合生成
- **WHEN** TMDB 返回中文主标题与英文原名/别名（如「斗罗大陆」与「Soul Land」）
- **THEN** 系统为该影视生成含多语言别名的关键词集合

#### Scenario: 别名缺失回退
- **WHEN** TMDB 未返回别名或别名为空
- **THEN** 使用主标题作为唯一关键词，不阻断搜索

### Requirement: 多关键词并行搜索

巡检搜索 SHALL 使用影视别名集合构造多组云盘搜索关键词并并行检索，合并结果作为候选集；不得仅用单一主标题关键词。

#### Scenario: 中英文关键词并行命中
- **WHEN** 对「斗罗大陆」巡检且别名集合含「Soul Land」
- **THEN** 中文与英文关键词均被用于搜索，英文名资源（Soul.Land.S02E167）能进入候选集

#### Scenario: 部分关键词无结果不阻塞
- **WHEN** 某组关键词无搜索结果而其他组有
- **THEN** 汇总其他组结果继续匹配，不丢弃已有候选

### Requirement: 季号校验与候选筛选

候选资源 SHALL 与目标季号严格一致（Sxx 匹配），标题匹配支持主标题/别名/英文原名的成员匹配而非子串宽松放行；季号不符或错配其它剧/其它季的候选 SHALL 被拒绝。

#### Scenario: 错季候选被拒
- **WHEN** 目标是「斗罗大陆 S02」，候选为「斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29」（季号 S01 与目标 S02 不符）
- **THEN** 该候选被拒绝，不进入任务队列

#### Scenario: 正确候选入选
- **WHEN** 候选为「Soul.Land.S02E167」且季号与目标一致、集号匹配缺失集
- **THEN** 该候选进入任务队列候选集并被评分排序

#### Scenario: 标题成员匹配
- **WHEN** 候选标题包含别名/原名/主标题任一成员（整词/前缀令牌匹配）
- **THEN** 该候选获匹配分参与排序；不含任何成员的候选被拒绝

#### Scenario: 无季号候选降级放行
- **WHEN** 候选标题解析不出季号（如按集连载的「凡人修仙传 (2020) 4K [更新190集]」）
- **THEN** 季号校验不拒绝该候选，仅按标题成员匹配与评分参与排序

#### Scenario: 多季缺失按季集合判定
- **WHEN** 缺失集横跨多个季（目标季集合含 S01 与 S02）
- **THEN** 候选季号属于目标季集合任一时即通过季号校验，不属于任一季才拒绝
```
