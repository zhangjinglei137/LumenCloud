---
comet_change: episode-status-and-detail-polish
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-11-episode-status-and-detail-polish
status: final
---

# Design Doc: episode-status-and-detail-polish（列表集数统计真实展示 + 详情进行中任务）

> 本文是 open 阶段 `design.md`（高层方案框架 D1-D3）的深度技术细化，覆盖现状核查结论、详细实现设计、数据流、边界条件与测试策略。OpenSpec delta spec（`media-status` / `media-detail-ui`）为需求契约事实源。

## 1. 背景与目标

列表卡「已有 0/xx 集」聚合缺失 Emby 实际入库维度（本系统队列状态滞后于 Emby 实际收录）；详情页缺少「当前进行中任务」区块；详情页「状态」下拉宽度过窄。详见 `proposal.md` Why。

## 2. 现状核查结论（代码事实）

### 2.1 后端列表聚合（`media.py`）

| 位置 | 行为 |
|------|------|
| `_DQ_ACTIVE_STATUSES`（media.py:44） | `("pending", "transferring", "downloading", "scrape", "library", "quota_wait")` |
| `_TQ_ACTIVE_STATUSES`（media.py:45） | `("pending", "probing", "ready")` |
| `list_media` 三表聚合（media.py:348-396） | download_queue ∪ task_queue ∪ episode_state 按 `(media_id, episode)` 键 `_bump` 去重，同键以 rank 最高为准（in_progress=3 > failed=2 > done=1 > 其他终态=0） |
| `_stats`（media.py:423-438） | `available = ep["done"]`（**仅本系统完成数，无 Emby 维度**）；`missing = total - done`；total 优先 `tmdb_cache.number_of_episodes` |
| `_parse_episode`（media.py:121） | episode "SxxExx" → (season, episode_number) |

关键事实：`_rank` 已持有每部影视的 episode→rank 映射，`rank==1` 的 episode 集合即「本系统已完成集」code 集，可直接用于与 Emby 集合并集去重，无需额外查询。

### 2.2 后端详情接口（`media.py get_media` :527）

| 位置 | 行为 |
|------|------|
| `in_emby_codes`（media.py:576-586） | 已通过 `find_emby_id` + `list_episodes` 构建 Emby 已入库集 code 集合；Emby 故障降级为空 set |
| `tmdb_episodes`（media.py:580-627） | 全集轴（优先 `episode_info_cache`，回源 `get_tv_all_episodes`），每集含 `air_date`；外部故障 → 字段省略 |
| `merged_episodes`（media.py:642-673） | 全集轴 + 本地状态合并，`resolve_episode_status` 归一 5 态 |
| 响应（media.py:730-748） | `**media_dto` + `media` + `episode_state` + `transfer_queue`；**无 active_tasks** |

### 2.3 Emby 服务（`emby.py`）

- `find_emby_id`（emby.py:232）：tmdb_id 精确匹配（AnyProviderIdEquals）→ title 模糊兜底，返回 Emby Item Id。
- `list_episodes`（emby.py:326）：`/Shows/{emby_id}/Episodes` → 已入库集列表（含归一 `code`）。
- **无进程内 TTL 缓存**。参照模式：tmdb.py `_SEASON_AIR_TTL=6h` / `_ALL_EPS_TTL=6h` 模块级 dict 缓存。

### 2.4 前端

| 位置 | 行为 |
|------|------|
| `episodeText`（MediaListView.vue:64-74） | movie → seriesStatusLabel；tv → `已有 ${avail} / ${total} 集`（avail=available ?? downloaded） |
| 状态下拉（MediaDetailView.vue:255-258） | `el-select v-model="form.status" size="small"`，无显式宽度 |
| `DOWNLOAD_QUEUE_STATUS_MAP`（format.ts:183-193） | pending/transferring/downloading/scrape/library/quota_wait/done/skipped/failed → [文案, type, 自定义色] |
| `TASK_QUEUE_STATUS_MAP`（format.ts:153-159） | pending/probing/ready/error/done → [文案, type, 自定义色] |
| `EpisodeStats`（types/index.ts:18-24） | available/total/missing/downloaded（松散可选字段） |

关键事实：两表状态字典 **`pending` 语义冲突**（task_queue「待探测」 vs download_queue「排队中」），active_tasks 必须携带来源标识供前端选字典。

## 3. 设计决策

### D1 列表「已有」= Emby 入库 ∪ 本系统已完成（去重）

**`emby.py` 新增 `get_ingested_episode_codes(tmdb_id, title) -> set[str]`：**

- 组合 `find_emby_id` + `list_episodes` → 归一 code 集合（`"SxxExx"`）。
- 进程内 TTL 缓存（模块级 dict，TTL **6h**，对齐 tmdb.py 模式）。
- 缓存 key 拼接 Emby 配置指纹（`base_url + api_key` 短哈希）→ 配置变化自然失效（tasks 1.2 要求）。
- `find_emby_id` 未命中（影视不在 Emby）→ 缓存空 set（TTL 内避免反复查询）。
- `EmbyUnavailable`（配置缺失/网络故障）→ **不缓存、上抛**，由调用方降级。
- 缓存值按集 code 升序排序后 join 存储，防重复插入顺序抖动。

**`media.py list_media._stats` 扩展：**

- 用现有 `_rank`（rank==1）构建每部影视的 `done_codes` 集合。
- 仅对 `tv + tmdb_id 存在 + missing>0`（`total > len(done_codes)`）的影视触发 `get_ingested_episode_codes`，`asyncio.gather` 并发（缓存命中时零网络开销）。
- `available = len(done_codes | emby_codes)`；`missing = max(0, total - available)`（防 TMDB 全集数滞后导致负值）。
- 任一部影视 Emby 查询失败 → 该影视 available 回退 `len(done_codes)`，接口不报错（spec「Emby 未配置回退」）。
- 不缓存 Emby 故障结果，下一轮列表请求重试。

**列表响应契约不变**：`episode_stats.available/missing` 语义从「本系统完成数」升级为「可观看数」，前端按新文案渲染。

### D2 详情 `active_tasks` 字段（后端直供，前端不推导）

**`get_media` 新增 `active_tasks`（仅 tv；movie 不返回该字段）：**

- 数据源：该 media 的 download_queue 行（`status in _DQ_ACTIVE_STATUSES`）∪ task_queue 行（`status in _TQ_ACTIVE_STATUSES`）。
- 每项输出 `{season, episode, status, source, air_date}`：
  - `season/episode`：`_parse_episode` 解析（解析失败 → 该项保留原 episode 字符串，season 为 null，按原 episode 展示，不丢弃）。
  - `status`：各自表原始 status 值。
  - `source`：`"dq" | "task"`（前端据此选 `downloadQueueStatusLabel` / `taskQueueStatusLabel`，D3）。
  - `air_date`：从 `tmdb_episodes` 全集轴构建 `(season, episode) -> air_date` 查找表（**覆盖所有季**，优于现有 air_map 只覆盖 episode_rows 所在季）；`tmdb_episodes` 缺失 → None。
- 过滤顺序：
  1. 终态剔除：两表 status 集合本身已限定进行中态（`_DQ/_TQ_ACTIVE_STATUSES` 不含 done/failed/skipped）。
  2. 已入库剔除：`code in in_emby_codes` → 剔除。Emby 故障时 `in_emby_codes` 为空 set → 全部保留（宽松，不误删任务展示）。
  3. 未到首播日剔除：`air_date` 已知且 `date.fromisoformat(air_date) > date.today()` → 剔除；air_date 未知 → 保留（宽松，对齐 design.md Risk）。
- 排序：`(season, episode)` 升序（season 为 null 的排最后）。
- 响应：`media_dto["active_tasks"] = [...]`；movie / 无进行中任务 → 空数组（前端按字段存在性 + 长度渲染）。

### D3 状态文案复用既有字典，不新建状态定义

- 前端 `active_tasks` 状态标签按 `source` 分支：
  - `"dq"` → `downloadQueueStatusLabel/Type/Color`（DOWNLOAD_QUEUE_STATUS_MAP）
  - `"task"` → `taskQueueStatusLabel/Type/Color`（TASK_QUEUE_STATUS_MAP）
- 不新建状态字典，避免展示层与队列层分叉。

### D4 前端展示

- **`MediaListView.episodeText`**：tv 且 `total` 已知 → `已有 N 缺失 M`（N=available，M=missing）；total 未知 → `已有 N 集`；movie → `seriesStatusLabel` 回退（现状保留）。
- **`MediaDetailView` 新增「当前进行中任务」区块**（紧凑 el-table，位于集数状态区上方）：
  - 列：集号（SxxExx）、集名（TMDB 名称，经 `episodeDisplayName`；无则「—」）、状态标签（D3）。
  - **空态：无任务时隐藏整个区块**（已确认）。
  - `v-if="detail.media_type !== 'movie' && activeTasks.length > 0"`。
- **状态下拉宽度**：`el-select` 显式 `style="min-width: 132px"`（选中「订阅中/已暂停」文本完整可见，spec「状态下拉完整显示选项」）。

## 4. 边界条件与降级矩阵

| 场景 | 行为 |
|------|------|
| Emby 未配置 / 不可达（列表） | `get_ingested_episode_codes` 抛 EmbyUnavailable → 该影视 available 回退 done 计数；列表接口不报错 |
| Emby 未配置 / 不可达（详情） | 既有 `in_emby_codes` 为空 set → active_tasks 全部保留、tmdb_episodes 字段省略 → air_date None 宽松通过 |
| 影视不在 Emby | 缓存空 set（6h）→ available 保持 done 计数 |
| TMDB 全集数滞后（Emby 已有集 > total） | `missing = max(0, total - available)` |
| 任务集 season 无法解析 | 保留原 episode 展示，season=null 排最后 |
| air_date 缺失 / 非法 | 不按首播日过滤，任务保留展示 |
| movie | 列表回退状态文案；详情不返回 active_tasks 字段 |

## 5. 数据契约（增量，无破坏性变更）

- `GET /api/media`：`episode_stats.available/missing` 语义升级（前端字段名不变）。
- `GET /api/media/{id}`：新增可选字段 `active_tasks: [{season, episode, status, source: "dq"|"task", air_date}]`；movie 不返回。
- 前端 `MediaDetail` 类型新增 `active_tasks?: ActiveTask[]`；`MediaItem` 的 `episode_stats` 字段沿用（available/missing 已声明）。

## 6. 测试策略

### 后端（pytest，`backend/tests/`）

1. `get_ingested_episode_codes`：
   - 命中缓存（mock 服务端一次调用，二次调用零网络）；配置指纹变化后缓存失效重新查询；不在 Emby → 空 set；EmbyUnavailable 上抛。
2. `list_media._stats`：
   - 已有口径：Emby 入库 3 集 + 本系统完成 2 集（无重叠）→ available=5；重叠去重（Emby 与 done 同集）→ 不重复计数。
   - Emby 故障回退：mock EmbyUnavailable → available=done 计数、接口 200。
   - `missing = max(0, total - available)` 防负。
   - 仅缺失影视触发 Emby 查询（total==done 时不调用 mock 服务）。
3. `get_media.active_tasks`：
   - 终态剔除（done/failed/skipped 不出现）；已入库剔除（in_emby_codes 命中）；未到首播日剔除（air_date 未来）；air_date 未知保留。
   - source/status 正确透传；season/episode 解析；排序升序；空数组；movie 无该字段。

### 前端（vitest，`frontend/src/`）

1. `episodeText`：tv+total 已知 → 「已有 N 缺失 M」；total 未知 → 「已有 N 集」；movie → seriesStatusLabel 回退。
2. `MediaDetailView`：active_tasks 区块渲染（集号 + 集名 + 按 source 的状态标签）；空态隐藏区块。
3. 状态下拉 min-width 样式断言。

## 7. 集成验证

- `npm run build` 与后端启动无报错；列表/详情页在本地环境正常展示（后端无真实 Emby 时回退不报错）。
