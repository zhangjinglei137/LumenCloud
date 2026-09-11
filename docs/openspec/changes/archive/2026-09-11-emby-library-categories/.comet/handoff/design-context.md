# Comet Design Handoff

- Change: emby-library-categories
- Phase: design
- Mode: compact
- Context hash: 33b04f27404cfeff6b8904a246047653302a1e14da808dc0b646bf4ff98396f7

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/emby-library-categories/proposal.md

- Source: docs/openspec/changes/emby-library-categories/proposal.md
- Lines: 1-26
- SHA256: ff28550c919db10caf92d4b85b88c0efcb2227e46e269928a046347004c0fb66

```md
## Why

Emby 影视库当前采用固定 Tab（电影/剧集/动漫）用 `item_type/status` 参数筛选，分类不准确：无法按真实媒体库（CollectionType）区分内容，动漫库识别依赖库名关键词匹配，且 Emby 原生推荐「先列媒体库（含 CollectionType）→ 再按媒体库查条目」的范式未被采用；列表分页造成浏览不便。

## What Changes

- **去掉分页**：Emby 影视库列表不再分页，一次性展示全部条目
- **媒体库类型接口**：采用 Emby 官方范式，新增「获取媒体库列表（含 CollectionType）」能力：
  - `GET /Library/MediaFolders`（用户视图，含 CollectionType/Id）作为媒体库列表数据源
  - 按媒体库 `CollectionType`（movies/tvshows/mixed 等）识别库类型
- **按媒体库类型获取影片详情**：以媒体库 Id 为 ParentId，`IncludeItemTypes`（Movie/Series）查询库内条目，替代现有 `item_type/status` 参数映射
- **修正分类**：前端 Tab 基于真实媒体库（CollectionType）分类，动漫库按库级识别而非仅库名关键词

## Capabilities

### New Capabilities
- `emby-library-categories`: Emby 影视库按媒体库类型（CollectionType）分类展示、库内条目查询与去分页能力

### Modified Capabilities
<!-- 现有 Emby 影视库展示（前端固定 Tab + 后端 item_type 映射）归类到新 capability；该能力此前无独立 spec，故无既有 capability 需求级变更。 -->

## Impact

- 后端：`backend/app/services/emby.py`（list_libraries/list_library 重构为媒体库类型查询）、`backend/app/routers/emby.py`（接口参数调整）
- 前端：`frontend/src/views/EmbyLibraryView.vue`（Tab 基于媒体库分类、去分页）、`frontend/src/stores/emby.ts`、`frontend/src/api`、`frontend/src/types`（EmbyLibraryQuery/Item 契约）
- 依赖：Emby 服务端（需支持 /Library/MediaFolders 与 /Items）；无数据库变更

```

## docs/openspec/changes/emby-library-categories/design.md

- Source: docs/openspec/changes/emby-library-categories/design.md
- Lines: 1-84
- SHA256: c86747816d4636782636974a938221f712b61a3d3ebb510facf168e2031a55cd

[TRUNCATED]

```md
# Design: emby-library-categories

## Context

现状（参见 proposal.md - Why）：
- 前端 `EmbyLibraryView.vue` 用固定 Tab（电影/剧集/动漫），走后端 `GET /api/emby/library?item_type=...&status=...&anime=true`
- 后端 `list_library` 用 `item_type/status/anime` 过滤 Emby `/Items`，动漫识别靠库名关键词（`_find_anime_library_item_id` → `ANIME_LIBRARY_KEYWORDS`）
- `GET /api/emby/libraries` 已存在（基于 /Library/VirtualFolders），但仅用于动漫识别，前端未展示媒体库
- 列表前端分页（client slice）造成浏览不便

外部调研（@librarian 核实 Emby 官方文档 dev.emby.media/reference/RestAPI.html）：
- Emby 官方推荐范式：① `GET /Library/MediaFolders`（或 `/Users/{UserId}/Views`）拿媒体库列表，每条含 `CollectionType` 与 `Id`；② `GET /Users/{UserId}/Items?ParentId=<ViewId>&IncludeItemTypes=Movie|Series&SeriesStatus=Continuing|Ended` 查库内条目
- 官方 API **没有 `collectionType` query 参数**，也没有 `ListLibrary` 端点；CollectionType 只在媒体库列表返回中携带
- `CollectionType` 取值：movies/tvshows/music/mixed/null 等；mixed/null 表示混合库
- 现有 `item_type/status` 需映射为 `IncludeItemTypes`/`SeriesStatus`（首字母大写）

## Goals / Non-Goals

**Goals**
- 去分页：Emby 影视库全量展示
- 媒体库列表接口（含 CollectionType）落地，前端基于真实媒体库分类
- 按媒体库（ParentId）+ IncludeItemTypes 查询条目，替代 item_type 猜测
- 动漫库库级识别

**Non-Goals**
- 订阅/入库流程改动（沿用现有 POST /api/media + scan）
- TMDB 匹配逻辑改动
- 后端 API 向后兼容旧 item_type 参数（仅前端升级，旧参数清理）

## Decisions

### D1：媒体库列表数据源采用 /Library/MediaFolders

**决策**：媒体库列表改用 `GET /Library/MediaFolders`（用户视图，含 Id/CollectionType/Name），而非现有 /Library/VirtualFolders。

**理由**：MediaFolders 返回的条目可直接作为 `/Items` 的 ParentId（VirtualFolders 的 ItemId 需额外关联）；含 CollectionType；官方「Browsing the Library」教程即此范式。

**备选**：保留 VirtualFolders → 字段更全（Locations/RefreshStatus）但需要 ItemId↔Id 映射，且无用户视图语义，否决。

### D2：后端新增「媒体库 + 库内条目」查询契约

**决策**：后端新增 `GET /api/emby/libraries`（返回 id/name/collection_type）与改造 `GET /api/emby/library`（新增 `library_id` 参数，内部映射 IncludeItemTypes/SeriesStatus；`item_type/status` 旧参数废弃移除）。

**理由**：后端封装 Emby 两步范式，前端无需感知 ParentId/IncludeItemTypes 细节；错误归一（_to_http_exc）复用。

**备选**：前端直连 Emby API → 凭据暴露、错误处理重复，否决。

### D3：前端 Tab 基于媒体库分类

**决策**：EmbyLibraryView 顶部改为「电影 / 剧集 / 动漫 / 全部」分类，数据源为媒体库列表：
- 电影 = CollectionType==movies 的库，查 Movie
- 剧集 = CollectionType==tvshows 且非动漫库，查 Series（可带 SeriesStatus 过滤）
- 动漫 = tvshows 库中库名含动漫关键词（或显式配置）的库
- 全部 = 混合浏览（mixed 库或全部库合并）
- 每个分类下可选具体媒体库（若同类型多库）

**理由**：分类与 Emby 真实库结构对齐；动漫库级识别优于全局 Name 匹配；多库场景可下钻。

**备选**：继续固定 Tab + 全局关键词 → 多库/混合库分类必然错乱，否决。

### D4：去分页实现

**决策**：后端列表接口不限制 Limit（或使用 Emby 默认全量），前端移除分页组件；条目多时浏览器滚动。

**理由**：用户明确要求去分页；Emby 库条目量级（数百）全量返回可接受。

## Risks / Trade-offs

- [MediaFolders 受用户权限影响（某些库不可见）] → 使用系统 api_key 调用属管理端可见性；若需用户级视图可换 /Users/{UserId}/Views，当前不阻塞
- [mixed 混合库分类归属] → 「全部」分类统一承载 mixed/null 库，避免错分
- [旧 item_type/status 参数清理影响存量前端] → 本 change 同时升级前端调用方，接口单版本化（不保留兼容层）
- [动漫识别仍依赖库名关键词] → 关键词集已存在且可扩展；库级识别（限定 tvshows 库范围内匹配）已比全局匹配精确

## Migration Plan

1. 后端：emby service 新增 list_library_folders（MediaFolders）+ 改造 list_library（library_id/IncludeItemTypes）
2. 后端：router 暴露新参数，移除旧 item_type/status（前端同步改）
3. 前端：EmbyLibraryView 分类重构 + 去分页 + store/api/types 契约更新
4. 验证：真实 Emby 环境多库分类正确、全量展示无分页
5. 回滚：还原前端与后端旧契约（无数据库变更，回滚成本低）

```

Full source: docs/openspec/changes/emby-library-categories/design.md

## docs/openspec/changes/emby-library-categories/tasks.md

- Source: docs/openspec/changes/emby-library-categories/tasks.md
- Lines: 1-24
- SHA256: b5edda2cda92065eb956daf5d67031d3a087f9fedef91062934f1f74fdaeda2d

```md
# Tasks: emby-library-categories

## 1. 后端：媒体库列表接口（含 CollectionType）

- [ ] 1.1 services/emby.py 新增 `list_library_folders()`：调用 `GET /Library/MediaFolders` 返回 [{id, name, collection_type}]（复用 _get/_check_config/错误归一），验证 MediaFolders 返回解析正确且未配置/不可达抛 EmbyUnavailable
- [ ] 1.2 routers/emby.py 改造 `GET /api/emby/libraries`：返回 `{libraries: [{id, name, collection_type}], total}`，验证响应契约与前端类型一致

## 2. 后端：按媒体库查询库内条目

- [ ] 2.1 services/emby.py 改造 `list_library(library_id, item_type, status)`：以 library_id 为 ParentId 调用 `/Items`，IncludeItemTypes 映射（movie→Movie / series→Series），status 映射 SeriesStatus=Continuing|Ended，移除 anime 全局关键词路径，验证参数映射与 /Items 调用正确
- [ ] 2.2 routers/emby.py 更新 `GET /api/emby/library`：新增必选 `library_id` 参数，移除 `anime` 旧参数（保留 `item_type`/`status`，前端同步），验证接口契约更新
- [ ] 2.3 补充后端测试：MediaFolders 解析、IncludeItemTypes/SeriesStatus 映射、错误归一（未配置/不可达），验证 `pytest` 通过

## 3. 前端：分类重构与去分页

- [ ] 3.1 types/index.ts + api 更新 EmbyLibraryQuery（library_id 替代 item_type/status/anime）与 EmbyLibraryItem/Library 类型，验证类型契约与后端一致
- [ ] 3.2 stores/emby.ts 新增 fetchLibraries（媒体库列表）并调整 fetchLibrary(library_id)，验证 store 状态与错误码处理保留
- [ ] 3.3 EmbyLibraryView 分类重构：顶部「电影/剧集/动漫/全部」基于媒体库 CollectionType 生成，动漫库库级识别（tvshows 内关键词），电影/剧集库可下钻具体库，验证分类与真实媒体库一致
- [ ] 3.4 EmbyLibraryView 移除分页组件，列表全量展示，验证无分页控件且滚动浏览正常
- [ ] 3.5 前端测试补充：分类生成（多库/混合库/动漫库）、去分页数据流用例，验证 `vitest` 通过

## 4. 验证

- [ ] 4.1 手动验证真实 Emby 环境：多媒体库分类正确、按库查询条目正确、无分页全量展示，验证 `npm run build` 与后端启动无报错

```

## docs/openspec/changes/emby-library-categories/specs/emby-library-categories/spec.md

- Source: docs/openspec/changes/emby-library-categories/specs/emby-library-categories/spec.md
- Lines: 1-73
- SHA256: 6a2c6d260b74d15bd792da2e0ef0f5b22d4d73b4e0942886f0e5cedffe9cc4ef

```md
## Purpose

为 Emby 影视库提供按媒体库类型（CollectionType）分类的展示能力：获取媒体库列表（含类型），按媒体库查询库内影片/剧集条目，前端去分页浏览，分类准确反映 Emby 真实库结构。

## ADDED Requirements

### Requirement: 获取媒体库列表含类型

系统 SHALL 通过 Emby 官方接口（`GET /Library/MediaFolders`）获取媒体库列表，返回每个媒体库的名称、Id、CollectionType（movies/tvshows/mixed 等）与动漫标记 is_anime；tvshows 媒体库的 is_anime SHALL 由后端按库级关键词识别（库名含动漫关键词）；Emby 服务不可用时返回约定的 503 错误码（emby_not_configured / emby_unreachable）。

#### Scenario: 成功获取媒体库列表
- **WHEN** 用户打开 Emby 影视库且 Emby 服务可用
- **THEN** 接口返回媒体库列表，每项含名称、Id、CollectionType、is_anime

#### Scenario: 动漫库标记
- **WHEN** Emby 存在 tvshows 媒体库且库名含动漫关键词（动漫/动画/anime）
- **THEN** 该库的 is_anime 为 true；非 tvshows 媒体库的 is_anime 恒为 false

#### Scenario: 白名单过滤剧集库
- **WHEN** emby_series_library_ids 白名单配置非空
- **THEN** 媒体库列表仅返回白名单内的 tvshows 库，非 tvshows 库不受影响

#### Scenario: Emby 未配置
- **WHEN** EMBY_BASE_URL / EMBY_API_KEY 未配置
- **THEN** 返回 503 且 code=emby_not_configured，前端呈现「未配置空态」

#### Scenario: Emby 不可达
- **WHEN** Emby 网络故障或非 2xx 响应
- **THEN** 返回 503 且 code=emby_unreachable，前端呈现「不可达错误态」

### Requirement: 按媒体库类型查询库内条目

系统 SHALL 按所选媒体库查询库内条目：客户端以必选的媒体库 Id（library_id）作为 ParentId 调用 Emby `/Items` 接口，按所选类型映射 IncludeItemTypes（movie→Movie，series→Series，缺省 Movie,Series）获取影片/剧集；查询结果供前端展示与「加入订阅」。

#### Scenario: 按电影库查询影片
- **WHEN** 用户选择 CollectionType=movies 的媒体库并以 movie 类型查询
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Movie 查询并返回影片条目列表

#### Scenario: 按剧集库查询剧集
- **WHEN** 用户选择 CollectionType=tvshows 的媒体库并以 series 类型查询
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Series 查询并返回剧集条目列表

#### Scenario: 缺省类型查询全部
- **WHEN** 用户查询混合库（mixed/null）且不指定类型
- **THEN** 系统以该库 Id 为 ParentId、IncludeItemTypes=Movie,Series 查询并返回全部条目

#### Scenario: 剧集状态过滤
- **WHEN** 用户筛选在更/完结剧集
- **THEN** 系统以 SeriesStatus=Continuing|Ended 过滤剧集条目

### Requirement: 影视库列表去分页

Emby 影视库条目列表 SHALL 一次性返回全部匹配条目，不进行分页；前端展示全部结果。

#### Scenario: 全量展示
- **WHEN** 用户查看某媒体库条目
- **THEN** 列表展示全部条目，无分页控件，无需翻页

### Requirement: 分类准确反映媒体库类型

前端 Emby 影视库分类 SHALL 基于媒体库真实 CollectionType 与库级识别，而非仅依赖库名关键词或固定类型猜测；动漫库识别以库级识别为准（库名关键词作为辅助）。

#### Scenario: 按 CollectionType 分类
- **WHEN** 用户浏览 Emby 影视库
- **THEN** 分类标签与真实媒体库 CollectionType 对应（电影库/剧集库），不把剧集误归电影或反之

#### Scenario: 混合库归「全部」
- **WHEN** Emby 存在 CollectionType=mixed 或 null 的混合媒体库
- **THEN** 混合库条目归入「全部」分类展示，不进入电影/剧集分类

#### Scenario: 动漫库识别
- **WHEN** Emby 存在动漫媒体库（is_anime=true）
- **THEN** 动漫分类正确识别该库，不因固定类型猜测而错分

```
