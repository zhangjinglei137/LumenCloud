---
comet_change: emby-library-categories
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-11-emby-library-categories
status: final
---

# Design Doc: Emby 影视库按媒体库类型分类 + 去分页

- Date: 2026-09-10
- Status: confirmed
- 上游：`docs/openspec/changes/emby-library-categories/`（proposal/design/spec/tasks）

## 1. 背景与目标

Emby 影视库当前用固定 Tab（电影/剧集/动漫）配合后端 `item_type/status/anime` 参数筛选，分类不准确、动漫识别靠库名全局关键词、前端客户端分页不便浏览。本 change 采用 Emby 官方「先列媒体库（含 CollectionType）→ 再按媒体库查条目」范式，实现按真实媒体库分类展示、按库查条目、去分页全量浏览。

## 2. 契约设计

### 2.1 `GET /api/emby/library`（改造）

参数：

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `library_id` | str | **是** | 目标媒体库 Id（`/Users/{UserId}/Views` 返回的 Id，即 ViewId），作为 `/Items` 的 ParentId |
| `item_type` | `movie`/`series` | 否 | 映射 IncludeItemTypes：movie→Movie，series→Series；缺省 Movie,Series |
| `status` | `continuing`/`ended` | 否 | 映射 SeriesStatus；非空时保证 IncludeItemTypes 含 Series |

**移除** `anime` 参数（动漫能力由前端基于 is_anime 标记 + library_id 实现）。

响应保持 `{items, total, item_type}`（item_type 回显保留）。

### 2.2 `GET /api/emby/libraries`（改造）

数据源：`GET /Users/{UserId}/Views`（用户视图，含 Id/CollectionType/Name；返回的 Id 即 ViewId，可直接作为 `/Items` 的 ParentId）。UserId 通过 `GET /Users`（系统 api_key 管理员权限）获取并缓存。

响应：

```json
{
  "libraries": [
    {
      "id": "3b2f...",              // Views.Id（ViewId，与 VirtualFolders.ItemId 等价）
      "name": "电影",
      "collection_type": "movies",  // movies/tvshows/mixed/null 等
      "is_anime": false             // 后端判定：tvshows 且库名含动漫关键词
    }
  ],
  "total": 1
}
```

- `is_anime`：仅对 `collection_type == "tvshows"` 的库执行 `ANIME_LIBRARY_KEYWORDS` 库名匹配；其他类型固定 false。
- **白名单过滤**：`emby_series_library_ids` 配置非空时，`collection_type == "tvshows"` 的库仅保留 Id 命中白名单者；其他类型库不受影响。
- 仅保留影视类媒体库：`LIBRARY_COLLECTION_TYPES`（movies/tvshows/mixed/null）过滤逻辑沿用。

## 3. 后端实现设计

### 3.1 `list_library_folders()`（services/emby.py 新增）

```python
async def list_library_folders() -> list[dict[str, Any]]:
    """查媒体库列表（/Users/{UserId}/Views），返回 [{id, name, collection_type, is_anime}]。
    tvshows 库附加 is_anime 判定；白名单非空时过滤 tvshows 库。"""
```

- 先获取用户 Id：`GET /Users`（系统 api_key 管理员权限）返回用户数组，取首个用户的 `Id`（惰性缓存，复用 `_get_server_id` 模式）
- 调用 `_get("/Users/{user_id}/Views", {})`（复用鉴权/错误归一；未配置抛 EmbyUnavailable）；UserId 获取失败降级返回空列表并 warn
- Views 返回 dict 包装（`Items` 键），防御性兼容裸数组；每条 `Id` 即 ViewId（与 VirtualFolders.ItemId 等价，可直接作 `/Items` 的 ParentId）
- 过滤 `collection_type` 不在 `LIBRARY_COLLECTION_TYPES` 且非 null 的库
- is_anime 判定：`collection_type == "tvshows"` 时对 `Name` 做 `ANIME_LIBRARY_KEYWORDS` 大小写不敏感子串匹配
- 白名单过滤：读 `config_store.get("emby_series_library_ids")`，非空时 tvshows 库仅保留 `id in whitelist`（ViewId 与旧 VirtualFolders ItemId 等价 → 存量配置直接兼容）
- 记录日志 `Emby 媒体库列表: %d 个（影视类）`

### 3.2 `list_library(library_id, item_type, status)`（services/emby.py 改造）

```python
async def list_library(
    library_id: str,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
```

- `library_id` 必选，直接作为 `/Items` 的 `ParentId`
- **删除** anime 分支（`_find_anime_library_item_id` 及其调用）与 Q2 逐库合并循环（白名单职责移至 libraries 接口过滤，list_library 只做单库查询）
- IncludeItemTypes 映射保留：movie→Movie / series→Series / 缺省 Movie,Series；status 非空时补 Series
- `_fetch_items` 翻页拉取保留（全量语义，`_LIST_PAGE_SIZE`/`_LIST_MAX_PAGES` 防御不变）
- 后续管道不变：`_normalize_library_item` → `_attach_tmdb_series_status` → `_attach_in_media_flag`

### 3.3 router（routers/emby.py 改造）

- `/library`：`library_id: str = Query(...)` 必选；`item_type: Literal["movie","series"] | None`；`status: Literal["continuing","ended"] | None`；删除 `anime`
- `/libraries`：调用 `list_library_folders()`，返回 `{libraries, total}`
- 错误归一 `_to_http_exc` 不变

### 3.4 影响面检查

- `_find_anime_library_item_id`：删除（唯一调用方是 list_library anime 分支）
- `ANIME_LIBRARY_KEYWORDS`：保留，供 is_anime 判定
- `list_libraries`（VirtualFolders 版本）：被 `list_library_folders` 替代；检查是否有其他调用方（设置页白名单逻辑走新接口）
- 设置页契约：`EmbyLibraryFolder.item_id` → `id`（前端 types 同步）；`emby_series_library_ids` 存量配置按 Id 语义复用（真实环境验证兼容性，不一致时用户可在设置页重新选择）

## 4. 前端实现设计

### 4.1 types（types/index.ts）

```ts
export interface EmbyLibraryQuery {
  library_id: string          // 必选：目标媒体库 Id
  itemType?: EmbyItemType     // 可选：movie/series，缺省全部
  status?: EmbySeriesStatus   // 可选：continuing/ended
}

export interface EmbyLibraryFolder {
  id: string                  // 原 item_id → id（/Users/{UserId}/Views 的 Id，即 ViewId）
  name: string
  collection_type: string | null
  is_anime: boolean           // 新增：后端判定
}

export interface EmbyLibrariesResponse {
  libraries: EmbyLibraryFolder[]
  total: number
}
```

### 4.2 api（api/index.ts）

- `listEmbyLibraryApi(params)`：传 `library_id` + `itemType`/`status`；移除 `anime` 映射
- `listEmbyLibrariesApi()`：返回类型升级为含 `is_anime`

### 4.3 store（stores/emby.ts）

- `fetchLibrary({library_id, itemType, status})`：透传参数；错误码解析（`parseEmbyErrorCode`）保留
- 新增 `libraries` state 与 `fetchLibraries()`（或并入现有动作）：保存媒体库列表供分类派生
- 分类派生逻辑（computed）：按 `collection_type` + `is_anime` 分组
  - 电影 = movies 库
  - 剧集 = tvshows 且 !is_anime
  - 动漫 = is_anime
  - 全部 = mixed/null

### 4.4 EmbyLibraryView.vue

- 4 分类 Tab：数据源为媒体库列表（`libraries` state），动态生成
- 默认聚合视图：选中分类后，对该分类下所有库**并发/顺序**调用 `fetchLibrary(library_id)`，前端合并去重（按 emby_id）；提供媒体库下拉可下钻到单库
- **移除分页**：删除 `currentPage`/`pageSize`/`pagedItems`/分页相关 watch；卡片墙渲染 `filteredItems` 全量
- 保留：关键字过滤、纳入状态过滤、订阅/TMDB 搜索/批量订阅、海报 fallback、在 Emby 中打开
- 「全部」Tab：对 mixed/null 库逐一请求（item_type 缺省查全部类型）
- 动漫 Tab：请求 is_anime=true 库，item_type 缺省（查 Movie,Series 全部）

## 5. 边界条件与错误处理

| 场景 | 行为 |
|------|------|
| EMBY_BASE_URL / API_KEY 未配置 | 503 `emby_not_configured`（未配置空态），各接口一致 |
| Emby 不可达 / 非 2xx / JSON 异常 | 503 `emby_unreachable`（不可达错误态） |
| library_id 不存在 / 无权限 | Emby /Items 返回空 → 空列表，前端空态（不视为错误） |
| 白名单为空 | 全部 tvshows 库可见（现状行为） |
| 分类下无库 | Tab 禁用/空态提示 |
| 聚合请求部分失败 | 逐库请求独立 catch，失败库跳过并提示（不阻断其他库） |
| 动漫库不在白名单 | 不可见（配置即控制，文档说明） |
| mixed/null 库 | 仅「全部」分类承载 |
| `_fetch_items` 超过页数上限 | 日志警告 + 提前停止（防御，沿用现状） |

## 6. 测试策略

### 后端（pytest）
- `list_library_folders`：Views 解析（dict 包装/裸数组）、UserId 获取、Id（ViewId）/CollectionType/is_anime 判定（tvshows 关键词命中/非 tvshows 不判）、白名单过滤（空/非空）、未配置/不可达抛 EmbyUnavailable
- `list_library`：library_id 作为 ParentId 透传、IncludeItemTypes 映射（movie/series/缺省/status 补 Series）、SeriesStatus 映射、anime 参数已移除（router 层验证）
- router：新参数契约（library_id 必选、item_type/status 可选）

### 前端（vitest）
- 分类生成：多库/混合库/动漫库（is_anime 标记）分组正确
- 聚合视图：逐库请求合并去重
- 去分页：无分页控件、全量渲染
- api 参数序列化：library_id/itemType/status 正确映射

### 手动验证
- 真实 Emby：多库分类正确、按库查询正确、白名单可见性、无分页全量展示、`npm run build` 与后端启动无报错

## 7. 兼容性与迁移

- **旧 item_type/status 参数**：保留（接口单版本化，仅移除 anime）
- **设置页 item_id → id**：前端类型同步升级；存量 `emby_series_library_ids` 按 Id 语义复用，真实环境验证；不一致通过设置页重新选择修复
- **回滚**：还原前端与后端契约改动（无数据库变更，回滚成本低）

## 8. 实施顺序（对应 tasks.md）

1. 后端 `list_library_folders` + router `/libraries`（1.1、1.2）
2. 后端 `list_library` 改造 + router `/library`（2.1、2.2）+ 后端测试（2.3）
3. 前端 types/api/store（3.1、3.2）
4. 前端 EmbyLibraryView 分类重构 + 去分页（3.3、3.4）+ 前端测试（3.5）
5. 手动验证（4.1）
