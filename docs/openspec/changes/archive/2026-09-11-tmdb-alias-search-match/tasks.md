# Tasks: tmdb-alias-search-match

## 1. TMDB 别名数据源

- [x] 1.1 services/tmdb.py 读取 also_known_as/original_title（search_multi 与详情同步路径），归一化（小写/去标点/去后缀）后保存别名集合到 tmdb_cache（新增列或 JSON 扩展），验证已有影视记录含别名、缺失时回退主标题
- [x] 1.2 为既有影视补充别名回填路径（巡检首轮或后台任务触发），验证存量记录可获得别名
- [x] 1.3 补充测试：别名解析保存、别名缺失回退、归一化边界，验证 `pytest` 通过

## 2. 多关键词并行搜索

- [x] 2.1 tasks/scan.py `_build_keywords` 重构：由 [{title} Sxx] 扩展为主标题/别名/原名的多关键词候选（限数量防膨胀），验证关键词集合覆盖中英文
- [x] 2.2 services/cloudsaver.py search 支持多关键词并行检索并合并去重，验证各关键词结果合并、单组无结果不阻塞
- [x] 2.3 补充测试：关键词构造、并行合并、季号后缀继承（别名 + Sxx），验证 `pytest` 通过

## 3. 候选匹配与季号校验

- [x] 3.1 tasks/scan.py 新增季号解析与硬校验：目标季号 vs 候选标题季号（S01≠S02 拒绝；标题无季号时按目标 S01/整季资源降级），验证错季候选（The.Peerless.Tang.Clan.S01E29）被拒
- [x] 3.2 `_share_title_relevant`/`_rank_candidates` 重构：标题令牌群与别名集合作成员/前缀匹配评分（原名命中、别名命中、年份加权），替代宽松子串放行，验证 Soul.Land.S02E167 入选并优先
- [x] 3.3 回归测试：新增「斗罗大陆」场景用例（错部/错季拒绝、英文名命中）+「star」旧用例回归，验证 `pytest` 通过且既有行为不退化

## 4. 集成验证

- [x] 4.1 真实环境（或模拟 cloudSaver）端到端：对「斗罗大陆」触发巡检，确认续作错季资源不落任务、Soul.Land 正确候选入队；`npm run build` 通过