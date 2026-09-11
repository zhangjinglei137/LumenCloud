---
comet_change: emby-library-all-poster
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-11-emby-library-all-poster
status: final
---

# Design Doc: Emby「全部」聚合浏览修复 + 封面代理加载

- Date: 2026-09-11
- Status: confirmed
- 上游：`docs/openspec/changes/emby-library-all-poster/`（proposal/design/spec/tasks）

## 1. 背景与目标

Emby 影视库选择「全部」类型时列表为空（诊断 `GET /api/emby/library?library_id=7` 返回 `{items: [], total: 0, item_type: null}`）；同时 Emby 封面由 `<img>` 直连带 `api_key` 的 Emby 服务地址，整页刷新大量并发回源、且泄露 api_key。

**现状核查结论（代码已核实）：**

1. **「全部」空列表的直接根因在前端分组**：`frontend/src/stores/emby.ts` 的 `libraryGroups` getter 将 `movies` 分入电影组、`tvshows` 分入剧集/动漫组，**「全部」分组只收到 `mixed`/`null` 库**。`EmbyLibraryView.vue` `fetchCurrent()` 聚合态遍历 `libraryGroups['all']`，当环境中只有 movies/tvshows 库（最常见）时 `libs.length === 0` → 直接空态。
2. **封面直连**：`backend/app/services/emby.py` `_normalize_library_item`（:378）生成 `poster_url = {base}/Items/{item_id}/Images/Primary?api_key={api_key}`，前端 `<img>` 直连 Emby 并携带 api_key。
3. 现有 `poster.py` 代理已具备登录态 + SSRF/路径校验 + 进程内 TTL(600s) + Cache-Control(86400)，仅放行 `/t/p/` 前缀（TMDB 图床）。

**目标**（用户已确认 2026-09-11）：后端新增全部聚合端点，一次请求遍历全部影视类库并发拉取、去重、归一化；前端「全部」Tab 改为单次请求。Emby 封面 `poster_url` 由后端改为代理地址，api_key 不再出后端。分类 Tab（movie/series/anime）与单库下钻行为完全不变。

## 2. 契约设计

### 2.1 `GET /api/emby/library/all`（新增）

| 参数 | 类型 | 必选 | 说明 |
|------|------|------|------|
| `item_type` | `movie`/`series` | 否 | 与单库端点同语义：映射 IncludeItemTypes；缺省 Movie,Series |
| `status` | `continuing`/`ended` | 否 | 映射 SeriesStatus；非空时保证 IncludeItemTypes 含 Series |

响应与单库端点一致：`{"items": [...], "total": n, "item_type": "movie"|"series"|null}`（item_type 回显保留）。

错误契约与单库端点完全一致（`_to_http_exc` 复用）：配置缺失 → 503 `emby_not_configured`；不可达/全部库失败 → 503 `emby_unreachable`。

路径选择理由：独立路径 `/library/all` 保持既有 `/library` 契约（`library_id` 必选）不变，向后兼容；路由不冲突（不同路径）。

### 2.2 `poster_url` 契约变更（改造）

`EmbyLibraryItem.poster_url` 由「Emby 直连完整 URL」改为「后端代理地址」：

```text
旧: {emby_base}/Items/{item_id}/Images/Primary?api_key={key}
新: /api/poster?p=emby/{item_id}/Primary
```

- 不再内嵌 api_key；前端 `<img>` 同源请求代理（登录态由 cookie 兜底，与 TMDB 代理一致）。
- 无海报仍为 `null`，前端占位兜底不变。

## 3. 后端实现设计

### 3.1 公共参数构造（services/emby.py 抽公共辅助）

`list_library` 与 `list_all_library` 共用 IncludeItemTypes/SeriesStatus 口径，抽为公共辅助避免口径漂移：

```python
def _build_library_params(
    item_type: Optional[str], status: Optional[str], parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造 /Items 查询参数：IncludeItemTypes 映射 + SeriesStatus + 分页 + 可选 ParentId。"""
    include_item_types = {"movie": "Movie", "series": "Series"}.get(item_type or "", "Movie,Series")
    if status and "Series" not in include_item_types:
        include_item_types = f"{include_item_types},Series"
    params: dict[str, Any] = {
        "Recursive": "true",
        "IncludeItemTypes": include_item_types,
        "Fields": "ProviderIds,CommunityRating,ProductionYear,SeriesStatus",
        "Limit": str(_LIST_PAGE_SIZE),
    }
    if parent_id:
        params["ParentId"] = parent_id
    if status:
        params["SeriesStatus"] = status
    return params
```

`list_library` 改为调用该辅助（`parent_id=library_id`），行为不变。

### 3.2 `list_all_library(item_type, status)`（services/emby.py 新增）

```python
async def list_all_library(
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
```

流程：

1. **取全部影视类库**：复用 `list_library_folders()`（movies/tvshows/mixed/null；tvshows 白名单过滤沿用——即「全部」聚合范围与分类 Tab 可见范围一致）。配置缺失时该函数抛 `EmbyUnavailable("未配置")` → 上抛保持 `emby_not_configured`。空库列表 → 返回 `[]`（前端空态）。
2. **逐库并发拉取**：新增模块级信号量 `_LIBRARY_FETCH_CONCURRENCY = 3`（Emby 单实例，防止并发打爆；低于 TMDB 批处理 5 的取值）。每个库用 `_fetch_items(_build_library_params(item_type, status, parent_id=lib_id))` 分页拉取。
3. **错误隔离**：逐库独立 catch——单个库失败只记 warn 并计入失败数，不中断其他库。
4. **归一化 + 去重**：`_normalize_library_item` 归一化（复用，含 poster_url 代理格式改造），按 `emby_id` 去重（`dict[emby_id] → item`，保留先到者；Emby ItemId 为全局 GUID，跨库不重复是常态，去重仅为防御多视图/重复收录）。
5. **后处理**：复用 `_attach_tmdb_series_status`（批量连载判定，内部已有 `_TMDB_BATCH_CONCURRENCY` 信号量）与 `_attach_in_media_flag`（本地收录标记）。
6. **错误语义汇总**：
   - 配置缺失 → 抛 `EmbyUnavailable`（`emby_not_configured`）
   - 全部库失败（result 为空且失败数 > 0）→ 抛 `EmbyUnavailable`（`emby_unreachable`，取首个失败原因）
   - 部分失败 → 返回成功部分 + `logger.warning("Emby 全部聚合部分库失败: %d/%d", ...)`

### 3.3 端点 `GET /api/emby/library/all`（routers/emby.py 新增）

```python
@router.get("/library/all")
async def library_all(
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        items = await list_all_library(item_type, status)
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {"items": items, "total": len(items), "item_type": item_type}
```

### 3.4 封面代理扩展（services/poster.py + routers/poster.py）

**校验（`_validate_poster_path` 扩展）**：除 `/t/p/` 前缀外放行 `/emby/<itemId>/Primary` 形态：

- `itemId` 白名单字符集 `[A-Za-z0-9-]`（Emby ItemId 为 GUID 或数字串），长度 ≤ 64；
- 固定后缀 `/Primary`（当前唯一需要的 ImageType，不开放参数化）；
- 沿用 normpath 归一化复核 + 协议段（`://`）/反斜杠/null 字节拒绝——回源 URL 由「配置 base + 固定路径 + 白名单 itemId」拼接，**不接收任意 URL**，与 TMDB 前缀同等 SSRF 防护强度。

**回源（`fetch_poster` 按前缀分支）**：

```text
p.startswith("/emby/") → base = config_store.get("emby_base_url", settings.EMBY_BASE_URL)
                          url = {base}/Items/{item_id}/Images/Primary?api_key={key}
p.startswith("/t/p/")  → 原 TMDB 图床逻辑（镜像优先）
```

- emby 配置读取在 poster.py 内独立完成（`config_store.get` + `settings` 回退，复用 emby.py `_base_url` 语义但**不 import emby 模块**，避免服务层循环依赖；误填防御（无 scheme 的 host:port）与 `_base_url` 同规则，抽公共 `_require_config_url(key, fallback, hint)` 或本地复刻，实现取简单复刻）。
- 未配置（base 或 api_key 缺失）→ 抛 `PosterUnavailable`（路由映射 503）。
- 网络异常/非 2xx → 抛普通 `Exception`（路由映射 502），沿用 `_alert` 节流告警。
- 缓存：key 即 `p` 参数原文，`/emby/` 与 `/t/p/` 前缀天然隔离；TTL 沿用 600s、上限 100、失败不写缓存——全部沿用现有机制，不新增。
- Cache-Control：路由层已统一 `public, max-age=86400`，无需改动。

**routers/poster.py**：无需改动（校验/回源/异常映射全部在服务层）。

### 3.5 `_normalize_library_item` poster_url 改造（services/emby.py）

```python
poster_url = None
if has_poster:
    poster_url = f"/api/poster?p=emby/{item_id}/Primary"
```

- 不再需要 `base`/`api_key` 拼图（签名保留 `base` 参数——`emby_web_url` 仍依赖 base；`api_key` 参数删除）。
- `emby_web_url` 逻辑不变（仍为 Emby 直连 Web 详情地址，仅打开新窗口跳转用，不承载图片流量，保持现状不在本 change 范围）。

## 4. 前端实现设计

### 4.1 API 函数（frontend/src/api/index.ts 新增）

```typescript
/** 全部影视库聚合查询（GET /api/emby/library/all）；item_type/status 可选 */
export function listAllEmbyLibraryApi(params: {
  itemType?: EmbyItemType
  status?: EmbySeriesStatus
}): Promise<EmbyLibraryResponse> {
  const query: Record<string, string> = {}
  if (params.itemType) query.item_type = params.itemType
  if (params.status) query.status = params.status
  return http.get<EmbyLibraryResponse>('/emby/library/all', { params: query }).then((r) => r.data)
}
```

### 4.2 `EmbyLibraryView.vue` fetchCurrent 改造

聚合态分支按分类分流：

- `category === 'all'` 且未下钻 → **单次调用 `listAllEmbyLibraryApi({ status })`**（itemType 不传），成功置 `store.items`，失败按 `parseEmbyErrorCode` 置错误态；
- `movie`/`series`/`anime` 聚合态 → 保持现有逐库请求 + emby_id 去重逻辑（行为不变）；
- 下钻态（`selectedLibraryId` 非空）→ `fetchSingle` 不变。

注意：「全部」Tab 的下钻下拉数据源 `categoryLibraries`（`libraryGroups['all']`，仅 mixed/null）保持不变——下钻的是「混合库」分组，与原行为一致，不在本 change 范围；「全部」聚合态的正确性由新端点保证。

### 4.3 封面加载

- `<img :src="item.poster_url">` 直接使用后端返回的代理地址（已是 `/api/poster?p=emby/...`），**前端不做任何组装**（posterUrl() 仅服务 TMDB 流）；
- `posterErrors` 占位兜底逻辑保留（代理 502/503/网络失败时 `<img>` error 事件触发占位）。

### 4.4 store（stores/emby.ts）

不改动。`fetchCurrent` 走视图直接调 API（与现有聚合态模式一致），store 仅承接 `items/error/loading` 状态。

## 5. 数据流

```text
全部 Tab（未下钻）
  EmbyLibraryView.fetchCurrent
    → GET /api/emby/library/all?status=
      → list_all_library
        → list_library_folders()               # 全部影视类库（含白名单）
        → asyncio.gather(信号量 3, 逐库 _fetch_items)  # /Items 分页
        → _normalize_library_item（poster_url 代理格式）+ emby_id 去重
        → _attach_tmdb_series_status + _attach_in_media_flag
      → {items, total, item_type}
    → store.items → filteredItems（关键字/纳入本地过滤）→ 渲染

封面加载
  <img src="/api/poster?p=emby/{itemId}/Primary">
    → routers/poster.get_poster（登录态）→ _validate_poster_path（emby 分支）
    → fetch_poster（缓存命中 | 回源 {emby_base}/Items/{id}/Images/Primary?api_key=）
    → 200 图片 + Cache-Control: public, max-age=86400
```

## 6. 边界条件与错误处理

| 场景 | 行为 |
|------|------|
| 无影视类库（libraries 空） | `list_all_library` 返回 `[]`，前端空态；不抛错 |
| 单个库失败（部分失败） | 保留成功部分 + warn 日志；前端正常展示成功部分 |
| 全部库失败 | 503 `emby_unreachable`，前端错误态 + 全局拦截器提示 |
| Emby 未配置 | 503 `emby_not_configured`（`list_library_folders` 上抛），前端未配置空态 |
| 代理请求非法 itemId（字符集/长度/路径穿越/协议段） | 400 非法海报路径 |
| 代理回源时 Emby 未配置 | 503（PosterUnavailable） |
| 代理回源网络失败/非 2xx | 502 + 节流告警；前端 `<img>` error → 标题占位，不破版 |
| 代理缓存 | 600s TTL、上限 100、失败不写缓存（沿用） |
| 代理未登录 | 401（沿用 `get_current_user`） |
| 同一 emby_id 出现在多库 | 去重保留先到者（防御性；Emby ItemId 全局唯一） |
| 大库分页 | 沿用 `_LIST_PAGE_SIZE=500` / `_LIST_MAX_PAGES=40` 防御 |

## 7. 测试策略

**后端 pytest（新增/扩展）：**

- `test_emby_library_all.py`（新增）：
  - 多库聚合：mock `/Users/{id}/Views` + 多库 `/Items` 分页 → 归一化正确、emby_id 去重
  - mixed 库收录（collection_type=mixed 的库条目进入结果）
  - 部分失败：1 库失败 1 库成功 → 返回成功部分、不抛
  - 全部失败 → `EmbyUnavailable`
  - 未配置（无 api_key/base）→ `EmbyUnavailable("未配置")`
  - 端点契约：`/api/emby/library/all` 响应 `{items, total, item_type}`、item_type 回显
- `test_emby_library_folders.py`（扩展）：`list_library` 经 `_build_library_params` 重构后行为不变（回归）
- `test_poster_proxy.py`（扩展）：
  - `/emby/<合法 id>/Primary` → 200 图片 + Cache-Control；回源 URL 正确拼装（含 api_key 但不含在响应中）
  - 非法 itemId（`../`、`://`、非法字符、超长、多余路径段）→ 400
  - emby 未配置 → 503；未登录 → 401
- `_normalize_library_item` 单测：poster_url 为代理格式、无 api_key、无海报 null

**前端 vitest（扩展）：**

- `embyLibraryView.test.ts`：全部 Tab 聚合态调用 `listAllEmbyLibraryApi`（单次请求、不逐库）；封面失败占位兜底不回归
- `api` 相关测试（如存在）：`listAllEmbyLibraryApi` query 组装（item_type/status 可选）

**集成验证（任务 4.1）：** 真实 Emby 环境：全部类型列表非空；封面经代理加载、页面无 Emby 直连请求、无 api_key 泄露；`npm run build` 通过；`pytest` 全绿。

## 8. Spec 覆盖评估

现有 delta spec 验收场景已完整覆盖本设计，**无 Spec Patch 需要回写**：

- `emby-library-browse`：全部类型聚合非空、mixed 收录、Emby 不可达错误提示、封面代理显示/失败降级/不暴露 api_key ✓
- `poster-proxy`：前端统一走代理加载海报（含 Emby 封面）、URL 不携带 api_key、代理不可达优雅降级 ✓

## 9. 交付物清单

- 后端：`backend/app/services/emby.py`（`_build_library_params`、`list_all_library`、`_LIBRARY_FETCH_CONCURRENCY`、`_normalize_library_item` poster_url）、`backend/app/routers/emby.py`（`/library/all`）、`backend/app/services/poster.py`（`_validate_poster_path` emby 分支、`fetch_poster` 前缀分支）
- 前端：`frontend/src/api/index.ts`（`listAllEmbyLibraryApi`）、`frontend/src/views/EmbyLibraryView.vue`（fetchCurrent 分流）
- 测试：`backend/tests/test_emby_library_all.py`（新）、`backend/tests/test_poster_proxy.py`（扩展）、`frontend/src/views/embyLibraryView.test.ts`（扩展）
