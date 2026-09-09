---
comet_change: episode-status-cache
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-09-episode-status-cache
status: final
---

# Design Doc: episode-status-cache（深度技术设计）

> 本文是 open 阶段 `design.md`（高层方案框架）的深度技术细化，覆盖详细实现设计、数据流、边界条件与测试策略。OpenSpec delta spec（episode-cache / media-status）为需求契约事实源。

## 1. 目标与非目标

**目标**
1. 影视列表「已有 x/xx 集」与详情「集数状态」显示真实数据（当前恒 0 或不全）
2. 集信息（每集标题/首播日期）落库持久化 + 每日定时刷新，替代进程内 TTL 缓存作为持久数据源
3. 标记状态机统一：已在库 / 巡检中 / 异常（开播未下载成功含未搜到资源）/ 未开播
4. 单集大小逐行真实值（禁止共享占位）
5. 入库完成后自动更新集数状态

**非目标**
- 影视详情页 UI 布局重设计（media-detail-ui 负责：转存队列移除、header 布局、100 集分组 tag、TMDB 集名展示）
- 巡检/下载队列页面重构（queue-inspection-rework 负责）
- Emby 库分类、设置页、Docker 时区（各自独立 change）

## 2. 现状分析与根因

### 2.1 「已有 0/xx 集」与「集数状态(0)」根因

`media.py` 列表聚合（`list_media`）：
- `total` = task_queue ∪ download_queue ∪ episode_state 三表按 (media_id, episode) 去重后的集数 —— **只统计有队列/状态记录的集**，未巡检/未入队的集不计入，全集数不完整
- `available`(=`done`) = 三表合并后 rank==1（status=done）的集数 —— **判定口径仅本地 done**，Emby 已收录但本地无 done 记录（或记录未回填）的集不计入

`media.py` 详情（`get_media_detail`）：
- `episode_state` 列表只输出 download_queue 行 + episode_state 遗留行（**有记录才有行**），无记录的集不出现
- `tmdb_episodes` 依赖 `get_tv_all_episodes` 进程内 6h TTL 缓存，进程重启失效

### 2.2 「大小都一样」根因

详情 `_dq_episode_dto` 已按 `row.file_size` 逐行取值（正确），但疑似在**数据写入侧**：部分任务建行时 file_size 未回填真实值，或前端某处展示共享了首行值。**需在 build 阶段先归因**（核查转存/探测完成后 file_size 回填路径），本设计保证「展示侧逐行取本行 file_size、缺失显 '—'」，并顺带修复数据回填。

### 2.3 既有资产复用

- `TmdbCache`：影视级标量缓存（title/poster/tv_status/**number_of_episodes**）—— total 数据源
- `get_tv_all_episodes(tmdb_id)`：已实现「TV 全部正片季每集信息」回源（season/episode/air_date/name），zh-CN、降级语义良好 —— 每日刷新复用
- `get_tv_season_air_dates`：单季首播日期（进程内 6h 缓存）—— 状态机「未开播」判定可复用
- `library_check._finalize_done`：入库闭环唯一 finalize 点 —— 入库联动挂载点
- 三表（download_queue/task_queue/episode_state）episode 维度状态 —— 状态机输入

## 3. 技术方案

### 3.1 D1：集信息缓存表 `episode_info_cache`

**表结构**（alembic 手写迁移，双后端兼容约定）：

| 列 | 类型 | 约束 |
|---|---|---|
| id | BIG_PK Identity | 主键 |
| tmdb_id | Integer | NOT NULL |
| season | Integer | NOT NULL |
| episode | Integer | NOT NULL |
| name | Text | NULL（TMDB 未提供） |
| air_date | Text | NULL（"YYYY-MM-DD" 或 None） |
| updated_at | DateTime | server_default CURRENT_TIMESTAMP + onupdate |

- `UNIQUE(tmdb_id, season, episode)` 命名 `uq_episode_info_cache_tmdb_season_episode`
- 索引：`(tmdb_id)` 前缀满足唯一键即可（唯一约束自带索引）；如需按 media 维度刷新增 `media_id` 冗余列？—— **不冗余**，经 tmdb_id 关联（media.tmdb_id 已唯一）
- ORM：`EpisodeInfoCache`（mapped_column 非注解风格，规避 SQLAlchemy 2.0.36/Python3.14 兼容缺陷）

**为什么独立表而非 TmdbCache JSON 列**：TmdbCache 主键为 tmdb_id 字符串、存标量字段；集信息是列表结构需按集查询/增量更新。独立表 + 唯一键支持按集 upsert、按影视粒度刷新。

### 3.2 D2：集信息刷新入口与每日任务

**写缓存函数**（services/tmdb.py）：
```python
async def refresh_episode_info(tmdb_id: int) -> int:
    """回源 TV 全集信息并 upsert 到 episode_info_cache；返回写入行数。失败返回 0。"""
    episodes = await get_tv_all_episodes(tmdb_id)   # 复用现有回源（含降级）
    if not episodes:
        return 0
    # 逐行 upsert（唯一键冲突更新 name/air_date/updated_at）；批量一次提交
```
- 语义：**整体成功才更新**；对单个影视回源失败（返回 []）保留旧数据（upsert 不动旧行）
- 读缓存函数：
```python
async def get_episode_info(tmdb_id: int) -> list[dict]:
    """读 episode_info_cache，返回 [{season, episode, name, air_date}] 升序；无数据返回 []。"""
```

**定时任务**（scheduler.py）：
- job id：`episode_info_refresh`，`IntervalTrigger(hours=<interval>)`，默认 24h
- 配置键：`episode_info_refresh_interval_hours`（system_config），`get_job_enabled` 开关可停用
- 遍历：`SELECT id, tmdb_id FROM media WHERE media_type='tv' AND tmdb_id IS NOT NULL`，串行调用 refresh_episode_info，单影视异常捕获后 log warning 继续
- 与既有任务模式一致（参考 library_check / capacity_alert 的注册与开关读取）

### 3.3 D3：标记状态机 `resolve_episode_status`

集中函数（routers/media.py 或 services 层，供列表与详情复用）：

```python
def resolve_episode_status(
    local_status: str | None,      # download_queue.status / episode_state.state 归一后的本地态
    in_emby: bool,                 # Emby 已收录
    air_date: str | None,          # TMDB 首播日期（YYYY-MM-DD）
    now: date,
) -> str:
    """归一状态：in_library / error / scanning / not_aired / pending"""
    if in_emby or local_status == "done":
        return "in_library"
    if local_status in ("failed", "error", "unmatched"):   # 开播但失败/未搜到
        return "error"
    if local_status in _ACTIVE:      # queued/transferring/downloading/ready/probing 等
        return "scanning"
    if air_date and date.fromisoformat(air_date) > now:
        return "not_aired"
    return "pending"
```

**优先级**：已在库 > 异常 > 巡检中 > 未开播 > 待定。
- 异常优先于巡检中：同一集 failed 记录比 queued 记录更能反映「需要关注」
- 未开播判定：air_date 晚于今天 → not_aired（不参与巡检/下载）
- `in_emby` 为「详情页已查询 Emby 收录的代码集」；列表场景无 Emby 查询 → 仅本地 done 判在库（列表聚合为独立轻量路径，见 3.4）

**列表与详情的判定差异**：
- **列表**（episode_stats）：轻量聚合，available = 本地 done 数（3 表 rank==1），不查 Emby（避免 N+1 与外部依赖）。用户已确认「本地 done 或 Emby 收录」用于状态判定 —— 列表场景以本地 done 为准，Emby 收录通过 `media.in_emby`（影视级标记）与入库联动（3.6）间接体现
- **详情**（episode_state 合并视图）：每集可查 Emby 收录（in_emby_codes）+ 首播日期 → 完整状态机

### 3.4 列表 episode_stats 修正

`list_media` 聚合改造：
- `total`：优先 `tmdb_cache.number_of_episodes`（JOIN media.tmdb_id → tmdb_cache），**为空/NULL 时回退**三表聚合去重数
- `available`(=done)：保持三表 rank==1 统计（本地 done）—— 入库联动（3.6）会持续把已入库集置 done，随 D5 修复「迟迟不入库」后该值自然增长
- 输出契约不变：`episode_stats: {total, done, failed, in_progress, available, downloaded, missing}`
- `missing = total - done`（total 用 TMDB 全集数后更准确）

**前端展示**：`episodeText` 已有 `available / total` 逻辑，无需改结构；修正后端数值即可。

### 3.5 详情 episode_state 合并视图

`get_media_detail` 改造：
- 数据源：`tmdb_episodes`（优先读 `get_episode_info` 缓存 → 无则回源 `get_tv_all_episodes`）作为全集轴
- 每集输出（对齐现有 `_dq_episode_dto` 前端契约 + 状态机字段）：

```python
{
    "episode": "S01E01",            # 由 season/episode 生成（SxxExx，三位保留）
    "season": s, "episode_number": e,
    "name": <缓存 name>,             # 新增：集名称（media-detail-ui 展示用）
    "air_date": <缓存 air_date>,
    "state": resolve_episode_status(...),   # 归一状态（in_library/error/scanning/not_aired/pending）
    "status": <归一状态>,            # 前端契约别名
    "file_size": <本地行 file_size 或 None>,
    "size_gb": <file_size/GB 或 None>,
    "in_emby": bool,
    "file_name": <本地行或 None>,
    "updated_at": <本地行或 None>,
}
```
- **全集轴**：即使无本地记录，该集也有行（state 按 air_date → not_aired/pending）
- **本地状态合并**：以 download_queue 行优先（同 media+episode），回退 episode_state 遗留行；无本地行则本地字段为 None
- 排序：按 (season, episode) 升序（供 media-detail-ui 分组导航），不再按 updated_at desc
- `tmdb_episodes` 顶层字段保留（兼容），与 episode_state 合并视图共用集缓存

### 3.6 D4/D5：大小真实值与入库联动

**D4 大小**：
- 详情合并视图 `file_size` 逐行取本行 download_queue.file_size（回退 episode_state.file_size），缺失 None → 前端 '—'
- build 阶段调查「全部相同」根因：若为转存/探测完成后 file_size 未回填，在对应流程补回填（transfer 转存完成后写真实大小）

**D5 入库联动**：
- `library_check._finalize_done` 在 finalize 成功时，确保该集在 download_queue 为 done（现状已是）→ 列表聚合 rank==1 自动 +1
- 若需同步 episode_state（遗留表兼容）：finalize 时 upsert episode_state(state='done', file_size) 保持双表一致
- 状态机侧：该集 next 聚合时 resolve 出 in_library（本地 done），无需额外写标记字段

## 4. 数据流

```
TMDB API
   │ get_tv_all_episodes（每日任务 episode_info_refresh 或详情回源）
   ▼
episode_info_cache ──► get_episode_info ──► 详情 episode_state 合并视图（全集轴+名称+首播日期）
                                                │
download_queue/task_queue/episode_state ────────┤（本地状态+file_size）
                                                ▼
                                        resolve_episode_status
                                                ▼
                                列表 episode_stats / 详情集数状态（in_library/error/scanning/not_aired/pending）

入库完成：library_check._finalize_done → dq status=done → 列表聚合 +1 → in_library
```

## 5. 边界条件

| 场景 | 行为 |
|---|---|
| movie / 无 tmdb_id | 不查询集缓存；详情 episode_state 为空（电影无集概念）；列表 total 回退聚合或无 |
| TMDB 无集信息 / 回源失败 | 缓存为空，详情全集轴为空（回退有记录集），不阻断接口 |
| Emby 未配置/故障 | in_emby 全 False，状态机降级为「本地 done」判定在库，不阻断 |
| 集无 air_date | 不判未开播（None → pending/error 分支），按其他条件判定 |
| 同 media+episode 多表记录 | download_queue 优先，es 遗留行仅在无 dq 行时展示 |
| total 有 TMDB 数据但 done=0（新订阅未入库） | 显示 0/xx（正确反映现状）；入库联动后增长 |
| 集 100 集以上（SxxExx 三位） | episode key 三位保留（S01E100），与现有 _parse_episode 兼容 |

## 6. 测试策略

**后端 pytest**（tests/test_tmdb.py 扩展 + tests/test_media.py + tests/test_library_check.py）：
1. `refresh_episode_info`：写缓存行数、重复刷新 upsert 不重复、回源失败保留旧数据
2. `get_episode_info`：有缓存返回升序列表、空缓存返回 []
3. `resolve_episode_status`：状态优先级表驱动（done→in_library、failed→error、queued→scanning、air_date>now→not_aired、其余→pending）
4. 列表统计：构造 5 done / 20 total（tmdb number_of_episodes=20）断言 available=5 total=20；无 tmdb 数据回退聚合
5. 详情合并视图：全集轴齐全、本地状态合并、file_size 逐行真实、movie 返回空
6. 入库联动：finalize 后列表统计 +1、episode_state 双表一致

**前端 vitest**（format.ts / MediaListView）：
1. 归一状态文案与颜色映射（in_library/error/scanning/not_aired/pending）
2. episodeText：x/xx 正确渲染、无统计回退

## 7. 迁移计划

1. alembic 迁移：创建 episode_info_cache（唯一键+索引）
2. models 新增 EpisodeInfoCache ORM
3. services/tmdb.py：refresh_episode_info / get_episode_info
4. scheduler.py：注册 episode_info_refresh 任务 + 配置键
5. routers/media.py：列表 total 修正 + 详情合并视图 + resolve_episode_status
6. library_check.py：finalize 双表同步
7. 前端：format.ts 状态映射、MediaListView/MediaDetailView 展示适配
8. 部署：alembic upgrade → 首轮每日任务填充缓存 → 验证详情集信息非空、列表集数非 0

**回滚**：还原前端与路由改动、删除任务注册；episode_info_cache 表可安全保留（无副作用）。无既有数据迁移风险（只新增表/字段，不改列）。

## 8. 风险与缓解

| 风险 | 缓解 |
|---|---|
| TMDB 每日全量刷新限流 | 串行 + 失败跳过 + system_config 可调间隔；单影视失败不影响其余 |
| 详情合并视图 N+1 | 集缓存单次聚合查询 + in_emby 单次查询；无逐集外部调用 |
| 状态口径变更影响前端 | format.ts 同步扩展映射，未知状态回退原文 |
| 列表 total 用 TMDB 数据与本地不一致（TMDB 多了未开播集） | missing 反映真实差距；未开播集在详情标记 not_aired 解释 |
| 大小根因调查延迟 | 展示侧先保证逐行真实值（缺失'—'），数据回填作为独立子任务 |
| SQLAlchemy/Python3.14 兼容 | 沿用 mapped_column 非注解风格 |
