---
comet_change: tmdb-alias-search-match
role: technical-design
canonical_spec: openspec
---

# tmdb-alias-search-match 深度技术设计

## Context

搜索链路现状（代码已核实）：`search_multi`（tmdb.py:289-360）只读 `language=zh-CN` 主标题并 upsert `tmdb_cache`，不读 `also_known_as`/`original_title`；`get_by_tmdb_id`（tmdb.py:200-286）回源 `/3/{movie|tv}/{id}` 详情，`also_known_as` 字段当前未读取；`_build_keywords`（scan.py:693-709）用 `media.title + Sxx` 构造单组关键词；`_search_and_rank`（scan.py:872-908）串行 for 循环调用 `cloudsaver.search`，随后 `_share_title_relevant`（A1，scan.py:807-825）用「精确/前缀/子串」宽松放行，`_rank_candidates`（scan.py:716-737）权重为 title-in-name +10 / word-in-name +2 / year-in-name +5。

错误样例：目标「斗罗大陆 S02」时，`斗罗大陆Ⅱ绝世唐门.The.Peerless.Tang.Clan.S01E29`（标题含「斗罗大陆」子串被放行 + 季号 S01）被误收，正确英文名 `Soul.Land.S02E167`（不含中文关键词）被剔除。`TmdbCache` 模型（models/__init__.py:381-410）现无别名列。见 proposal.md - Why 与 open 阶段 design.md。

## Goals / Non-Goals

**Goals:**
- 为影视构建含中英文的多语言别名集合并持久化（tmdb_cache 新列），作为关键词与匹配依据
- 巡检搜索从单关键词改为多关键词并行检索（≤5 词）
- 引入季号硬校验与别名成员匹配，替代「标题子串宽松放行」
- 错误样例回归：`Soul.Land.S02E167` 命中；`The.Peerless.Tang.Clan.S01E29` 被拒

**Non-Goals:**
- 不引入 AI/LLM 挑选（保持确定性规则）
- 不改下载队列消费与任务流转
- 不处理 TMDB 搜索页人工挑选交互（TmdbSearch 组件不动）
- 不做别名的手动编辑 UI

## Decisions

### D1: 别名持久化进 tmdb_cache（新增 aliases 列）

- **方案**：`TmdbCache` 新增 `aliases`（Text/JSON 字符串列，存归一化别名列表）；`get_by_tmdb_id` 回源时读取 `original_title`/`original_name` + `also_known_as`（仅保留中英文及拉丁字符别名），归一化（小写、去空格、去版本后缀）后落库；返回 dict 附带 `aliases`。
- **理由**：巡检按固定间隔执行，不能每次实时调 TMDB；tmdb_cache 已有 7 天 TTL 且被 scan 直接复用（`_media_year`、A3 集号校验均走 `get_by_tmdb_id`），加列成本最低、存量记录随巡检 A3 `force_refresh` 自动回填。
- **备选**：独立别名表（规范化但多一次关联查询）；运行时实时拉取（慢、依赖外部）→ 均放弃。
- **注意**：`search_multi` 的 TMDB search 响应不含 `also_known_as`（该字段只在详情响应），别名仅在详情回源路径落库，不回填路径覆盖已落库别名。

### D2: 关键词构造多词并行，限制总量

- `_build_keywords` 扩展为：`[主标题]`（movie）或 `[主标题 Sxx...] + [主标题] + [original_title] + [original_title Sxx] + [优选英文 aka]`，总关键词数上限 5（`_MAX_SEARCH_KEYWORDS` 常量）；别名缺失时回退主标题。
- `_search_and_rank` 改 `asyncio.gather` 并行调用 `cloudsaver.search`，结果按 share 去重合并；单关键词失败 warning 继续，全部失败（ok==0）抛 `ScanSearchUnavailable`（既有降级语义保留）。
- **理由**：全量别名会放大 cloudSaver 调用；original_title 与高价值 aka（英文）覆盖绝大多数中英文混搜场景。

### D3: A1 从子串放行改为别名成员匹配 + 边界检查

- `_share_title_relevant` 重构：候选标题与 `[主标题] ∪ [别名集合]` 做成员匹配（归一化后整词/前缀令牌），并在命中处检查后续字符边界（后随分隔符/标点/年份/季号则放行；紧贴中文字符或罗马数字则拒绝）。
- 效果：`斗罗大陆Ⅱ绝世唐门` 中「斗罗大陆」后紧贴「Ⅱ」→ 拒绝；`凡人修仙传 (2020) 4K` 中「凡人修仙传」后随空格+括号 → 放行；`Soul.Land.S02E167` 对别名 `soul land` → 成员命中放行。
- **风险与兜底**：A1 收紧可能误拒部分紧贴续集资源，A2/A3 文件级校验（`_full_mode_accept`）继续兜底，且评分阶段对「以主标题开头但带版本后缀」的资源有年份加权，误杀可接受。

### D4: 季号硬校验 + 无季号降级

- 目标季集合 = `{_season_of_key(k) for k in missing_keys}`（缺集 key 解析，默认 1）；候选季号从候选标题解析（`_RE_SXXEXX`/新增季号正则，Sxx 缺省视为整季资源）。
- 规则：候选季号解析成功且 ∉ 目标季集合 → 拒绝；解析失败（无季号）→ 降级放行（仅标题匹配参与后续）。
- 缺失跨多季时按集合判定（任一命中即通过）。

### D5: 评分增量加权

- `_rank_candidates` 保留既有权重（主标题精确 +10、词命中 +2、年份 +5），新增：别名/原名精确命中 +8、季号与目标一致 +3（紧贴场景下优先正确季）。

## Risks / Trade-offs

- [A1 边界收紧误杀紧贴续集/合集资源] → A2/A3 文件级校验兜底 + 年份加权优先正确版本；回归用例覆盖「少帅」「凡人修仙传」防退化
- [别名质量依赖 TMDB 数据，个别影视别名缺失] → 别名缺失回退主标题，不阻断搜索；别名列表上限与归一化去重控制膨胀
- [并行搜索放大 cloudSaver 调用（≤5 词/影视/轮）] → 关键词上限常量 + 结果合并去重 + 单词失败降级，调用量可控
- [评分改动影响既有排序行为] → 保留既有权重结构，新增项为增量加权，回归测试验证
- [tmdb_cache 加列需迁移] → Alembic 新增迁移（aliases 可空列，无回填负担，读时兼容旧行）

## Migration Plan

1. 新增 Alembic 迁移：`tmdb_cache.aliases`（Text，nullable）
2. `get_by_tmdb_id` 回源路径写入 aliases；存量影视无需立即回填（巡检 A3 force_refresh 自动补全）
3. 搜索链路逐步替换：先 `_build_keywords`/`_search_and_rank`/`_share_title_relevant`/`_rank_candidates`，回归测试先行
4. 无破坏性变更：aliases 列可空，读路径对缺失值回退主标题

## Open Questions

无。