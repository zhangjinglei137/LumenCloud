# Brainstorm Summary

- Change: tmdb-alias-search-match
- Date: 2026-09-10

## 确认的技术方案

**数据流**：TMDB 别名落库 → 巡检多关键词并行搜索 → A1 成员匹配+季号校验 → 评分排序

1. **别名持久化（tmdb_cache 加列）**：`get_by_tmdb_id` 回源时读取 `original_title/original_name` + `also_known_as`，归一化后写入 `tmdb_cache.aliases`（JSON/Text 列，Alembic 迁移）；存量影视随巡检 A3 回源（force_refresh）自动回填；`search_multi` 返回不含 also_known_as（TMDB search 结果无该字段），别名仅详情路径落库。
2. **关键词构造（_build_keywords 扩展）**：movie→[主标题]；tv→ 季词[主标题 Sxx] + 主标题兜底 + 别名词（original_title、优选英文 aka，可带 Sxx 季词）；总量上限 5；别名缺失回退主标题。
3. **并行搜索（_search_and_rank 改造）**：`asyncio.gather` 并行 `cloudsaver.search` 多词，合并去重；单词失败 warning 继续（ok==0 抛 ScanSearchUnavailable 语义保留）。
4. **A1 成员匹配（_share_title_relevant 重构）**：主标题/别名集合成员匹配 + 后续字符边界检查（拒绝「斗罗大陆Ⅱ绝世唐门」中文/罗马数字紧贴；放行「凡人修仙传 (2020)」括号边界）；不再纯子串放行。
5. **季号校验（新逻辑）**：候选标题解析 Sxx → 与缺失集目标季集合比对；解析出季号且不匹配 → 拒绝；无季号候选降级放行（仅标题匹配）；缺失跨多季时按候选季 ∈ 缺失季集合判定。
6. **评分（_rank_candidates 扩展）**：主标题 +10 保留；原名/别名命中加分；年份 +5、词命中 +2 保留；季号一致可额外加权。

## 关键取舍与风险

- 边界字符检查可能收紧 A1（「少帅将我宠上天」类紧贴续集会被拒）→ 这正是目标（误匹配收窄）；A2/A3 文件级校验仍兜底，避免过严误杀
- 别名质量依赖 TMDB 数据 → 别名缺失回退主标题，不阻断搜索
- 并行搜索放大 cloudSaver 调用（≤5 关键词）→ 上限控制 + 合并去重
- 评分改动影响既有排序 → 保留既有主标题/年份/词权重，别名仅为增量加权

## 测试策略

- pytest：别名解析/落库/回填、关键词构造（上限 5、别名词）、并行合并、A1 边界用例（斗罗大陆Ⅱ vs Soul.Land）、季号校验（S01 vs S02、无季号降级、多季缺失）、评分排序
- 回归：现有「少帅」「凡人修仙传」用例不退化（A2/A3 兜底仍生效）
- 端到端：模拟 cloudSaver 对「斗罗大陆」触发巡检——The.Peerless.Tang.Clan.S01E29 被拒、Soul.Land.S02E167 入候选

## Spec Patch

- title-alias-search「季号校验与候选筛选」补充场景：无季号候选降级放行（仅标题匹配）；缺失跨多季时按候选季号 ∈ 缺失季集合判定
- 无其余 spec 变更