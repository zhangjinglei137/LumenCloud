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
