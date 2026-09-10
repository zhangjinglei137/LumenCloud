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