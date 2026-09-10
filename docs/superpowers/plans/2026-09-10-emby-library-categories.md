# emby-library-categories 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emby 影视库改为按媒体库类型（CollectionType）分类展示、按库（library_id）查询条目、前端去分页全量浏览。

**Architecture:** 后端将媒体库列表数据源从 `/Library/VirtualFolders` 换为 `/Library/MediaFolders`（含 Id/CollectionType），新增 `list_library_folders()` 返回 `{id, name, collection_type, is_anime}` 并对剧集库应用白名单过滤；`list_library` 改为必选 `library_id`（ParentId 单库查询），移除 anime 分支与逐库合并循环。前端按媒体库列表动态生成 4 分类 Tab（电影/剧集/动漫/全部），默认聚合同类库（逐库请求前端合并去重），移除客户端分页组件。

**Tech Stack:** Python/FastAPI/httpx/SQLAlchemy(async)，Vue 3/Pinia/element-plus/Vite，pytest，vitest。

**Spec:** `docs/superpowers/specs/2026-09-10-emby-library-categories-design.md`（design doc）、`docs/openspec/changes/emby-library-categories/specs/emby-library-categories/spec.md`（delta spec）、`docs/openspec/changes/emby-library-categories/tasks.md`（任务边界）

## Global Constraints

- 后端契约：`GET /api/emby/library` 参数为 `library_id`（必选）+ `item_type`（可选 movie/series）+ `status`（可选 continuing/ended）；**移除 `anime`**
- 后端契约：`GET /api/emby/libraries` 返回 `{libraries: [{id, name, collection_type, is_anime}], total}`
- `is_anime` 判定仅作用于 `collection_type == "tvshows"` 的库（库名含 ANIME_LIBRARY_KEYWORDS，大小写不敏感）；其他类型恒 false
- `emby_series_library_ids` 白名单非空时，仅过滤 `collection_type == "tvshows"` 的库；其他类型库不受影响
- 分类语义：电影=movies 库（item_type=movie）；剧集=tvshows 且 !is_anime（item_type=series）；动漫=is_anime（item_type 缺省查全部）；全部=mixed/null（item_type 缺省查全部）
- 分类 Tab 默认聚合同类库：前端遍历该类下所有库逐库请求、按 `emby_id` 合并去重；提供媒体库下拉可下钻单库
- 前端移除分页组件（`currentPage`/`pageSize`/`pagedItems`/分页 watch/`el-pagination`）；保留关键字过滤、纳入状态过滤、订阅/TMDB 搜索/批量订阅/海报 fallback/在 Emby 中打开
- 前端类型 `EmbyLibraryFolder.item_id` → `id`（设置页 SettingsView.vue 引用同步）；`EmbyLibraryQuery` 改为 `{library_id, itemType?, status?}`
- 删除不再被引用的后端函数：`list_libraries`（VirtualFolders 版）、`_find_anime_library_item_id`
- 后端测试模式沿用 `backend/tests/test_emby_server_id.py` 的 `_install_get`/`_db_maker`/`_use_test_db`/`run()` 模式
- 产物语言：zh-CN

---

## Task 1: 后端媒体库列表接口（1.1 + 1.2）

**Files:**
- Modify: `backend/app/services/emby.py`（新增 `list_library_folders`，替换 `list_libraries`）
- Modify: `backend/app/routers/emby.py:55-68`（`/libraries` 改调新函数）
- Test: `backend/tests/test_emby_server_id.py`（追加用例）或新建 `backend/tests/test_emby_library_folders.py`

**Interfaces:**
- Produces: `list_library_folders() -> list[dict[str, Any]]`，每项 `{id, name, collection_type, is_anime}`；未配置抛 `EmbyUnavailable`，网络故障降级返回 `[]`（与现 `list_libraries` 一致）
- Consumes: `emby_mod._get`、`config_store.get("emby_series_library_ids")`、`ANIME_LIBRARY_KEYWORDS`、`LIBRARY_COLLECTION_TYPES`

- [x] **Step 1: 写失败测试**

新建 `backend/tests/test_emby_library_folders.py`，复制现有测试辅助模式（`_install_get`、`_use_test_db` 可复用 test_emby_server_id.py 同款写法）：

```python
"""list_library_folders（/Library/MediaFolders）测试。"""
import asyncio
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.emby as emby_mod
from app.services.emby import EmbyUnavailable, list_library_folders


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture()
def _db_maker():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(emby_mod.Base.metadata.create_all)

    run(_create())
    yield maker
    run(engine.dispose())


def _use_test_db(monkeypatch, maker):
    monkeypatch.setattr(emby_mod, "async_session", maker)


def _install_get(monkeypatch: pytest.MonkeyPatch, handler: Callable):
    """替换 emby_mod._get：handler(path, params) -> payload dict。"""
    async def _fake_get(path: str, params: dict[str, Any], **kwargs):
        return handler(path, params)
    monkeypatch.setattr(emby_mod, "_get", _fake_get)


def _folder(fid: str, name: str, ct: Optional[str]) -> dict[str, Any]:
    return {"Id": fid, "Name": name, "CollectionType": ct}


def test_mediafolders_parsed_with_is_anime(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    def _handler(path, params):
        assert path == "/Library/MediaFolders"
        return {"Items": [
            _folder("m1", "电影", "movies"),
            _folder("t1", "剧集", "tvshows"),
            _folder("t2", "动漫番组", "tvshows"),
            _folder("x1", "混合", "mixed"),
        ]}

    _install_get(monkeypatch, _handler)
    result = run(list_library_folders())

    assert result == [
        {"id": "m1", "name": "电影", "collection_type": "movies", "is_anime": False},
        {"id": "t1", "name": "剧集", "collection_type": "tvshows", "is_anime": False},
        {"id": "t2", "name": "动漫番组", "collection_type": "tvshows", "is_anime": True},
        {"id": "x1", "name": "混合", "collection_type": "mixed", "is_anime": False},
    ]


def test_mediafolders_whitelist_filters_tvshows(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    monkeypatch.setattr(
        emby_mod.config_store, "get",
        lambda key, default=None: "t1" if key == "emby_series_library_ids" else default,
    )

    def _handler(path, params):
        return {"Items": [
            _folder("m1", "电影", "movies"),
            _folder("t1", "剧集", "tvshows"),
            _folder("t2", "另一个剧集", "tvshows"),
        ]}

    _install_get(monkeypatch, _handler)
    result = run(list_library_folders())

    assert [lib["id"] for lib in result] == ["m1", "t1"]


def test_mediafolders_not_configured_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_library_folders())
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_library_folders'`

- [x] **Step 3: 实现 `list_library_folders` 并删除 `list_libraries`**

在 `backend/app/services/emby.py` 中，用新实现替换现有 `list_libraries`（约 L361-390）：

```python
async def list_library_folders() -> list[dict[str, Any]]:
    """查媒体库列表（/Library/MediaFolders），返回 [{id, name, collection_type, is_anime}]。

    每个媒体库返回 MediaFolders 的 Id（可直接作为 /Items 的 ParentId）。
    is_anime：仅对 tvshows 库按 Name 含 ANIME_LIBRARY_KEYWORDS 判定（大小写不敏感）。
    emby_series_library_ids 白名单非空时，仅过滤 tvshows 库（其他类型不受影响）。
    MediaFolders 调用失败时记 warn 返回空列表，不阻断主流程；配置缺失仍抛
    EmbyUnavailable（保持前端「未配置空态」）。
    """
    try:
        payload = await _get("/Library/MediaFolders", {})
    except EmbyUnavailable as exc:
        if "未配置" in str(exc):
            raise
        logger.warning("Emby 媒体库列表获取失败（分类降级为空）: %s", exc)
        return []
    # MediaFolders 返回 dict 包装（Items 键）；防御性兼容裸数组
    raw_folders: Any = payload if isinstance(payload, list) else payload.get("Items")
    folders: list[Any] = raw_folders or []
    # 白名单：仅影响 tvshows 库
    whitelist_raw = (config_store.get("emby_series_library_ids") or "").strip()
    whitelist = {x.strip() for x in whitelist_raw.split(",") if x.strip()}

    result: list[dict[str, Any]] = []
    for folder in folders:
        collection_type = folder.get("CollectionType")
        if collection_type not in LIBRARY_COLLECTION_TYPES and collection_type is not None:
            continue
        folder_id = folder.get("Id")
        name = folder.get("Name") or ""
        is_anime = False
        if collection_type == "tvshows":
            if whitelist and folder_id not in whitelist:
                continue  # 白名单过滤剧集库
            lower_name = name.lower()
            is_anime = any(keyword in lower_name for keyword in ANIME_LIBRARY_KEYWORDS)
        result.append({
            "id": folder_id,
            "name": name,
            "collection_type": collection_type,
            "is_anime": is_anime,
        })
    logger.info("Emby 媒体库列表: %d 个（影视类）", len(result))
    return result
```

同时删除 `_find_anime_library_item_id`（约 L393-403，唯一调用方在 anime 分支，Task 2 移除）。

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -v`
Expected: 3 PASS

- [x] **Step 5: 更新 router `/libraries`**

`backend/app/routers/emby.py`：import 改为 `list_library_folders`，`/libraries` 端点调用新函数，docstring 更新：

```python
from app.services.emby import EmbyUnavailable, list_library, list_library_folders

@router.get("/libraries")
async def libraries(
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 媒体库列表（/Library/MediaFolders，含 CollectionType/is_anime）：
    供前端影视库分类 Tab 生成与设置页「剧集页可见媒体库」多选。"""
    try:
        libs = await list_library_folders()
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "libraries": libs,
        "total": len(libs),
    }
```

- [x] **Step 6: 回归运行既有 Emby 测试**

Run: `cd backend && python -m pytest tests/test_emby_server_id.py tests/test_emby_series_status.py tests/test_emby_aired.py -v`
Expected: 全部 PASS（若旧测试引用了已删除的 `list_libraries`/`_find_anime_library_item_id`，同步修正这些用例）

- [x] **Step 7: 提交**

```bash
git add backend/app/services/emby.py backend/app/routers/emby.py backend/tests/test_emby_library_folders.py
git commit -m "feat(emby): 媒体库列表改用 MediaFolders 并附加 is_anime 与白名单过滤"
```

---

## Task 2: 后端按库查询条目（2.1 + 2.2）

**Files:**
- Modify: `backend/app/services/emby.py`（`list_library` 改造，约 L528-630）
- Modify: `backend/app/routers/emby.py:34-52`（`/library` 参数契约）
- Test: `backend/tests/test_emby_library_folders.py`（追加 `list_library` 用例）

**Interfaces:**
- Consumes: `list_library_folders` 产出的 `id`（作为 ParentId）、Task 1 已删除 `_find_anime_library_item_id`
- Produces: `list_library(library_id: str, item_type: Optional[str] = None, status: Optional[str] = None) -> list[dict]`；router `/library` 接受 `library_id` 必选 + `item_type`/`status` 可选

- [x] **Step 1: 写失败测试**

在 `backend/tests/test_emby_library_folders.py` 追加：

```python
from app.services.emby import list_library


def _library_item(name, kind="Series", tmdb=1001):
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProductionYear": 2024,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ImageTags": {"Primary": "x"},
        "SeriesStatus": None,
    }


def test_list_library_passes_library_id_as_parent(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    captured: dict[str, Any] = {}

    def _handler(path, params):
        captured["path"] = path
        captured["params"] = params
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            return {"Items": [_library_item("A", kind="Movie", tmdb=11)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    result = run(list_library("m1", "movie", None))

    assert captured["path"] == "/Items"
    assert captured["params"]["ParentId"] == "m1"
    assert "Movie" in captured["params"]["IncludeItemTypes"]
    assert result[0]["emby_id"] == "id-A"


def test_list_library_maps_status_to_series_status(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    captured: dict[str, Any] = {}

    def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            captured["params"] = params
            return {"Items": [_library_item("S1")]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)

    run(list_library("t1", "series", "continuing"))

    assert captured["params"]["ParentId"] == "t1"
    assert captured["params"]["SeriesStatus"] == "continuing"
    assert "Series" in captured["params"]["IncludeItemTypes"]
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -v`
Expected: 新 2 用例 FAIL（TypeError: 缺 library_id 或 ParentId 断言失败）

- [x] **Step 3: 改造 `list_library`**

`backend/app/services/emby.py` `list_library`（约 L528-630）签名与主体改造：

```python
async def list_library(
    library_id: str,
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """查 Emby 影视库（des-3 Emby 展示页 / GET /api/emby/library）。

    参数:
        library_id: 必选，目标媒体库 Id（/Library/MediaFolders 的 Id），作为 /Items 的 ParentId
        item_type:  "movie" 电影 / "series" 剧集 / None 全部（Movie,Series）
        status:     "continuing" 仅在更 / "ended" 已完结；非空时加 SeriesStatus 并确保 IncludeItemTypes 含 Series
    返回:
        归一化条目列表（同现有字段，含 series_status/in_media/media_id/emby_web_url）
    异常:
        EmbyUnavailable: 配置缺失 / 请求失败
    """
    # IncludeItemTypes：按 item_type 选择；status 非空时须含 Series
    include_item_types = {"movie": "Movie", "series": "Series"}.get(item_type or "", "Movie,Series")
    if status and "Series" not in include_item_types:
        include_item_types = f"{include_item_types},Series"

    params: dict[str, Any] = {
        "Recursive": "true",
        "IncludeItemTypes": include_item_types,
        "Fields": "ProviderIds,CommunityRating,ProductionYear,SeriesStatus",
        "Limit": str(_LIST_PAGE_SIZE),
        "ParentId": library_id,
    }
    if status:
        params["SeriesStatus"] = status

    items = await _fetch_items(params)

    base = _base_url()
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY)
    server_id = await _get_server_id()
    result: list[dict[str, Any]] = []
    for item in items:
        normalized = _normalize_library_item(item, base, api_key, server_id)
        if normalized is not None:
            result.append(normalized)

    await _attach_tmdb_series_status(result)
    await _attach_in_media_flag(result)

    logger.info("Emby 影视库（library_id=%s, item_type=%s, status=%s）: %d 条", library_id, item_type, status, len(result))
    return result
```

删除内容：anime 分支（`parent_id` 定位逻辑）、Q2 白名单逐库合并循环（`lib_ids` 相关）、`_find_anime_library_item_id` 引用。`_fetch_items` 保留不动。

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py tests/test_emby_server_id.py -v`
Expected: 全部 PASS（注意 test_emby_server_id.py 中 `list_library()` 无参调用需改为 `list_library("m1")` 等）

- [x] **Step 5: 更新 router `/library`**

`backend/app/routers/emby.py`：

```python
@router.get("/library")
async def library(
    library_id: str = Query(...),
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 影视库展示：library_id 必选（目标媒体库）；item_type=movie/series；
    status=continuing 在更 / ended 完结（仅对剧集生效）。"""
    try:
        items = await list_library(library_id, item_type, status)
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "items": items,
        "total": len(items),
        "item_type": item_type,
    }
```

- [x] **Step 6: 更新既有引用旧签名的测试**

Run: `cd backend && python -m pytest tests/test_emby_server_id.py tests/test_emby_series_status.py tests/test_emby_aired.py -v`
Expected: 若失败，将无参/含 anime 参数的 `list_library(...)` 调用改为 `list_library(<library_id>)`，补齐 ParentId 相关的 mock 断言。全部 PASS。

- [x] **Step 7: 提交**

```bash
git add backend/app/services/emby.py backend/app/routers/emby.py backend/tests/test_emby_library_folders.py
git commit -m "feat(emby): list_library 改为按 library_id 单库查询并移除 anime 路径"
```

---

## Task 3: 后端测试补全（2.3）

**Files:**
- Test: `backend/tests/test_emby_library_folders.py`（追加错误归一与映射用例）

**Interfaces:**
- Consumes: Task 1/2 产出的 `list_library_folders`、`list_library` 最终签名

- [ ] **Step 1: 写补充测试**

追加以下用例：

```python
def test_mediafolders_network_failure_returns_empty(monkeypatch, _db_maker):
    """网络故障降级为空列表（不阻断分类展示）。"""
    _use_test_db(monkeypatch, _db_maker)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    _install_get(monkeypatch, _boom)
    assert run(list_library_folders()) == []


def test_list_library_unconfigured_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_library("m1"))


def test_list_library_default_item_types(monkeypatch, _db_maker):
    """缺省 item_type → IncludeItemTypes=Movie,Series。"""
    _use_test_db(monkeypatch, _db_maker)
    captured: dict[str, Any] = {}

    def _handler(path, params):
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            captured["params"] = params
            return {"Items": []}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _handler)
    run(list_library("x1"))
    assert captured["params"]["IncludeItemTypes"] == "Movie,Series"
    assert captured["params"]["ParentId"] == "x1"
```

- [ ] **Step 2: 运行测试**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -v`
Expected: 全部 PASS

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_emby_library_folders.py
git commit -m "test(emby): 补充 MediaFolders 解析、映射与错误归一用例"
```

---

## Task 4: 前端 types + api 契约更新（3.1）

**Files:**
- Modify: `frontend/src/types/index.ts:397-447`（EmbyLibraryQuery / EmbyLibraryFolder / EmbyLibrariesResponse）
- Modify: `frontend/src/api/index.ts:277-298`（listEmbyLibraryApi / listEmbyLibrariesApi）
- Modify: `frontend/src/views/SettingsView.vue:649,651`（item_id → id）
- Test: `frontend/src/api/emby.test.ts`（新建，参数序列化用例）

**Interfaces:**
- Produces: `EmbyLibraryQuery {library_id: string; itemType?: EmbyItemType; status?: EmbySeriesStatus}`；`EmbyLibraryFolder {id: string; name: string; collection_type: string | null; is_anime: boolean}`；`EmbyLibrariesResponse {libraries: EmbyLibraryFolder[]; total: number}`
- Consumes: 现有 `EmbyItemType`/`EmbySeriesStatus`/`EmbyLibraryResponse`

- [ ] **Step 1: 写失败测试**

新建 `frontend/src/api/emby.test.ts`：

```typescript
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { listEmbyLibraryApi, listEmbyLibrariesApi } from './index'

vi.mock('./http', () => ({ default: { get: vi.fn() } }))
import http from './http'
const mockedGet = vi.mocked(http.get)

describe('Emby API 契约', () => {
  beforeEach(() => mockedGet.mockReset())

  it('listEmbyLibraryApi 序列化 library_id/itemType/status', async () => {
    mockedGet.mockResolvedValue({ data: { items: [], total: 0, item_type: null } })
    await listEmbyLibraryApi({ library_id: 'm1', itemType: 'movie', status: 'continuing' })
    expect(mockedGet).toHaveBeenCalledWith('/emby/library', {
      params: { library_id: 'm1', item_type: 'movie', status: 'continuing' },
    })
  })

  it('listEmbyLibraryApi 缺省不传多余参数', async () => {
    mockedGet.mockResolvedValue({ data: { items: [], total: 0, item_type: null } })
    await listEmbyLibraryApi({ library_id: 'x1' })
    expect(mockedGet).toHaveBeenCalledWith('/emby/library', { params: { library_id: 'x1' } })
  })

  it('listEmbyLibrariesApi 返回 is_anime 字段', async () => {
    mockedGet.mockResolvedValue({
      data: { libraries: [{ id: 't1', name: '动漫', collection_type: 'tvshows', is_anime: true }], total: 1 },
    })
    const res = await listEmbyLibrariesApi()
    expect(res.libraries[0].is_anime).toBe(true)
    expect(mockedGet).toHaveBeenCalledWith('/emby/libraries')
  })
})
```

（若 `http` 模块导入路径不同，按项目实际调整 mock 目标；vitest 配置支持默认 globals，参考既有测试。）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/api/emby.test.ts`
Expected: FAIL（TypeScript 编译或断言失败，因 api 仍传 itemType/anime）

- [ ] **Step 3: 更新 types/index.ts**

```typescript
/** Emby 库查询参数（GET /api/emby/library，library_id 必选） */
export interface EmbyLibraryQuery {
  /** 目标媒体库 Id（/Library/MediaFolders 的 Id），必选 */
  library_id: string
  /** 类型筛选：movie/series，缺省全部 */
  itemType?: EmbyItemType
  /** 剧集状态筛选：continuing 仅在更 / ended 已完结（后端用 SeriesStatus 参数） */
  status?: EmbySeriesStatus
}

/** Emby 媒体库（/Library/MediaFolders，含 CollectionType/is_anime）：影视库分类与设置页多选选项 */
export interface EmbyLibraryFolder {
  /** MediaFolders 库 Id（可作 /Items 的 ParentId） */
  id: string
  name: string
  collection_type: string | null
  /** 动漫库标记：tvshows 库且库名含动漫关键词（后端判定） */
  is_anime: boolean
}
```

- [ ] **Step 4: 更新 api/index.ts**

```typescript
export function listEmbyLibraryApi(params: EmbyLibraryQuery) {
  const { library_id, itemType, status } = params
  const query: Record<string, string> = { library_id }
  if (itemType) query.item_type = itemType
  if (status) query.status = status
  return http
    .get<EmbyLibraryResponse>('/emby/library', { params: query })
    .then((r) => r.data)
}

export async function listEmbyLibrariesApi(): Promise<EmbyLibrariesResponse> {
  return http.get<EmbyLibrariesResponse>('/emby/libraries').then((r) => r.data)
}
```

- [ ] **Step 5: 更新 SettingsView.vue 引用**

`frontend/src/views/SettingsView.vue` L649/651：`:key="lib.item_id"` → `:key="lib.id"`；`:value="lib.item_id"` → `:value="lib.id"`。

- [ ] **Step 6: 运行测试与类型检查**

Run: `cd frontend && npx vitest run src/api/emby.test.ts && npm run build`
Expected: 测试 PASS；`vue-tsc --noEmit` 通过（SettingsView 等已同步）

- [ ] **Step 7: 提交**

```bash
git add frontend/src/types/index.ts frontend/src/api/index.ts frontend/src/views/SettingsView.vue frontend/src/api/emby.test.ts
git commit -m "feat(frontend): Emby 契约更新 library_id/is_anime 并同步设置页"
```

---

## Task 5: 前端 store 更新（3.2）

**Files:**
- Modify: `frontend/src/stores/emby.ts`（fetchLibrary 签名、新增 libraries state/fetchLibraries、分类派生）

**Interfaces:**
- Consumes: Task 4 产出的 `EmbyLibraryQuery`、`EmbyLibraryFolder`、`listEmbyLibrariesApi`
- Produces: `useEmbyStore` 新增 state `libraries: EmbyLibraryFolder[]`；actions `fetchLibrary(params)`、`fetchLibraries()`；computed 分类分组 `libraryGroups`

- [ ] **Step 1: 更新 store**

`frontend/src/stores/emby.ts`：

```typescript
import { defineStore } from 'pinia'
import { listEmbyLibrariesApi, listEmbyLibraryApi } from '../api'
import type { EmbyLibraryFolder, EmbyLibraryItem, EmbyLibraryQuery } from '../types'

export const useEmbyStore = defineStore('emby', {
  state: () => ({
    items: [] as EmbyLibraryItem[],
    loading: false,
    error: null as EmbyErrorCode,
    libraries: [] as EmbyLibraryFolder[],
    librariesLoading: false,
    librariesError: null as EmbyErrorCode,
  }),
  getters: {
    /** 分类 → 媒体库列表（电影/剧集/动漫/全部） */
    libraryGroups(state): Record<'movie' | 'series' | 'anime' | 'all', EmbyLibraryFolder[]> {
      const groups = { movie: [], series: [], anime: [], all: [] } as Record<string, EmbyLibraryFolder[]>
      for (const lib of state.libraries) {
        if (lib.collection_type === 'movies') groups.movie.push(lib)
        else if (lib.collection_type === 'tvshows' && lib.is_anime) groups.anime.push(lib)
        else if (lib.collection_type === 'tvshows') groups.series.push(lib)
        else groups.all.push(lib) // mixed / null
      }
      return groups as Record<'movie' | 'series' | 'anime' | 'all', EmbyLibraryFolder[]>
    },
  },
  actions: {
    async fetchLibraries(): Promise<void> {
      this.librariesLoading = true
      try {
        const res = await listEmbyLibrariesApi()
        this.libraries = res.libraries ?? []
        this.librariesError = null
      } catch (err) {
        this.libraries = []
        this.librariesError = parseEmbyErrorCode(err)
      } finally {
        this.librariesLoading = false
      }
    },
    async fetchLibrary(params: EmbyLibraryQuery): Promise<void> {
      this.loading = true
      try {
        const res = await listEmbyLibraryApi(params)
        this.items = res.items
        this.error = null
      } catch (err) {
        this.items = []
        this.error = parseEmbyErrorCode(err)
      } finally {
        this.loading = false
      }
    },
  },
})
```

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npm run build`
Expected: 通过（EmbyLibraryView 若引用 `store.fetchLibrary` 旧签名，Task 6 修复；本步可临时保留旧调用，若 vue-tsc 因必选 library_id 报错，属预期，Task 6 一并处理）

- [ ] **Step 3: 提交**

```bash
git add frontend/src/stores/emby.ts
git commit -m "feat(frontend): emby store 新增媒体库列表与分类派生"
```

---

## Task 6: 前端 EmbyLibraryView 分类重构 + 去分页（3.3 + 3.4）

**Files:**
- Modify: `frontend/src/views/EmbyLibraryView.vue`（script：分类 Tab/聚合/去分页；template：Tab 生成/库下拉/移除分页）

**Interfaces:**
- Consumes: Task 5 store 的 `libraryGroups`/`fetchLibraries`/`fetchLibrary`
- Produces: 4 分类 Tab UI + 媒体库下钻下拉 + 聚合视图 + 无分页全量渲染

- [ ] **Step 1: 改造 script 部分**

替换 `frontend/src/views/EmbyLibraryView.vue` 中类型筛选相关逻辑：

```typescript
/** 分类 Tab：全部 / 电影 / 剧集 / 动漫（基于媒体库真实 CollectionType + is_anime） */
type Category = 'all' | 'movie' | 'series' | 'anime'
const category = ref<Category>('all')
/** 下钻：当前分类下选中的具体媒体库 id，null=聚合该分类全部库 */
const selectedLibraryId = ref<string | null>(null)

onMounted(() => {
  store.fetchLibraries()
  fetchCurrent()
})

/** 当前分类对应的库列表（用于下拉下钻） */
const categoryLibraries = computed<EmbyLibraryFolder[]>(() => store.libraryGroups[category.value])

/** 按当前分类 + 下钻库 + 状态筛选发请求（聚合态逐库请求合并） */
async function fetchCurrent() {
  if (store.librariesError === 'not_configured') return
  const libs = categoryLibraries.value
  if (selectedLibraryId.value) {
    await fetchSingle(selectedLibraryId.value)
    return
  }
  if (libs.length === 0) {
    store.items = []
    store.error = null
    return
  }
  // 聚合：逐库请求、按 emby_id 合并去重
  store.loading = true
  try {
    const seen = new Map<string, EmbyLibraryItem>()
    for (const lib of libs) {
      const res = await listEmbyLibraryApi(buildQuery(lib))
      for (const item of res.items) {
        if (!seen.has(item.emby_id)) seen.set(item.emby_id, item)
      }
    }
    store.items = [...seen.values()]
    store.error = null
  } catch (err) {
    store.items = []
    store.error = parseEmbyErrorCode(err)
  } finally {
    store.loading = false
  }
}

/** 单库查询（下钻态） */
async function fetchSingle(libraryId: string) {
  const lib = store.libraries.find((l) => l.id === libraryId)
  if (!lib) return
  await store.fetchLibrary(buildQuery(lib))
}

/** 按分类/库类型构造查询参数 */
function buildQuery(lib: EmbyLibraryFolder): EmbyLibraryQuery {
  const query: EmbyLibraryQuery = { library_id: lib.id }
  if (category.value === 'movie') query.itemType = 'movie'
  else if (category.value === 'series') query.itemType = 'series'
  // anime / all：不传 itemType（查全部类型）
  if (statusFilter.value) query.status = statusFilter.value
  return query
}

function onCategoryChange() {
  selectedLibraryId.value = null
  fetchCurrent()
}
```

保留：`statusFilter`/`inclusionFilter`/`keyword`/`filteredItems`/`pendingItems`/订阅相关（`subscribe`/`onSubscribeClick`/`submitTmdbSubscribe`/`subscribeAll`）/`posterErrors`/`openInEmby`/`typeLabel`。**删除**：`typeFilter`/`currentPage`/`pageSize`/`pagedItems`/分页相关 watch（L33-34、L64-67、L70-81）。

注意：`fetchCurrent` 原为同步函数，改造后为 async；模板中的 `@click="fetchCurrent"` 与「刷新」按钮仍兼容。

- [ ] **Step 2: 改造 template**

- 类型筛选 radio-group 替换为分类 Tab + 下钻下拉：

```vue
<el-radio-group :model-value="category" size="small" @change="(v: Category) => { category = v; onCategoryChange() }">
  <el-radio-button value="all">全部</el-radio-button>
  <el-radio-button value="movie">电影</el-radio-button>
  <el-radio-button value="series">剧集</el-radio-button>
  <el-radio-button value="anime">动漫</el-radio-button>
</el-radio-group>
<el-select
  v-if="categoryLibraries.length > 1"
  :model-value="selectedLibraryId"
  placeholder="全部{{ { all: '', movie: '电影', series: '剧集', anime: '动漫' }[category] }}库"
  clearable
  size="small"
  style="width: 160px"
  @change="(v: string | null) => { selectedLibraryId = v; fetchCurrent() }"
>
  <el-option
    v-for="lib in categoryLibraries"
    :key="lib.id"
    :label="lib.name"
    :value="lib.id"
  />
</el-select>
```

- 卡片墙 `v-for="m in pagedItems"` → `v-for="m in filteredItems"`
- **删除** `el-pagination` 块（L418-432）
- 「已配置但库为空」空态条件 `store.items.length === 0` 保留

- [ ] **Step 3: 类型检查与构建**

Run: `cd frontend && npm run build`
Expected: `vue-tsc --noEmit` 与 `vite build` 通过

- [ ] **Step 4: 提交**

```bash
git add frontend/src/views/EmbyLibraryView.vue
git commit -m "feat(frontend): Emby 影视库分类重构、聚合视图与去分页"
```

---

## Task 7: 前端测试补充（3.5）

**Files:**
- Test: `frontend/src/views/embyLibraryView.test.ts`（新建，或按项目既有测试位置）

**Interfaces:**
- Consumes: Task 5/6 的 store 分类派生与视图行为

- [ ] **Step 1: 写测试**

```typescript
import { describe, expect, it } from 'vitest'
import { useEmbyStore } from '../stores/emby'
import type { EmbyLibraryFolder } from '../types'

function lib(id: string, name: string, ct: string | null, is_anime = false): EmbyLibraryFolder {
  return { id, name, collection_type: ct, is_anime }
}

describe('emby store 分类派生', () => {
  it('按 CollectionType/is_anime 分组：多库/混合库/动漫库', () => {
    const store = useEmbyStore()
    store.libraries = [
      lib('m1', '电影', 'movies'),
      lib('t1', '剧集', 'tvshows'),
      lib('t2', '动漫番组', 'tvshows', true),
      lib('x1', '混合', 'mixed'),
      lib('n1', '未分类', null),
    ]
    const groups = store.libraryGroups
    expect(groups.movie.map((l) => l.id)).toEqual(['m1'])
    expect(groups.series.map((l) => l.id)).toEqual(['t1'])
    expect(groups.anime.map((l) => l.id)).toEqual(['t2'])
    expect(groups.all.map((l) => l.id)).toEqual(['x1', 'n1'])
  })
})
```

（若需要组件级测试，可基于 store 层用例 + 视图手工验证覆盖；聚合请求逻辑建议抽为 store action 后单测，当前按 store getter 测试覆盖核心分类。）

- [ ] **Step 2: 运行测试**

Run: `cd frontend && npx vitest run src/views/embyLibraryView.test.ts`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
git add frontend/src/views/embyLibraryView.test.ts
git commit -m "test(frontend): 补充 Emby 分类派生测试"
```

---

## Task 8: 手动验证（4.1）

**Files:**
- 无代码文件；真实 Emby 环境验证 + 构建/启动检查

- [ ] **Step 1: 后端构建与测试全量**

Run: `cd backend && python -m pytest -v`
Expected: 全部 PASS（含既有用例回归）

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 通过

- [ ] **Step 3: 启动后端并验证接口契约**

启动后端（依赖用户环境），调用：

```bash
curl -s http://localhost:<port>/api/emby/libraries | python -m json.tool
# 期望：libraries[].id/name/collection_type/is_anime
curl -s "http://localhost:<port>/api/emby/library?library_id=<真实库Id>&item_type=movie" | python -m json.tool
# 期望：按库返回影片，无 item_type/anime 旧参数
```

- [ ] **Step 4: 真实 Emby 环境手动验证**

- 多媒体库分类正确（电影/剧集/动漫/全部 与 Emby 真实库一致）
- 按库查询条目正确（下钻具体库）
- 白名单可见性（设置页配置 emby_series_library_ids 后剧集库按预期过滤）
- 无分页全量展示（滚动浏览正常）
- 存量 emby_series_library_ids 按 Id 语义是否继续生效（不一致则提示用户在设置页重新选择）

- [ ] **Step 5: 记录验证证据并提交收尾**

确认构建/测试/手动验证均通过后（真实 Emby 验证由用户确认），提交剩余变更并勾选 tasks.md 全部任务。

```bash
git add -A
git commit -m "chore(emby): emby-library-categories 实施完成并验证"
```

---

## Self-Review（计划编写者自查）

- **Spec 覆盖**：spec 的 4 个 Requirement（媒体库列表含类型/按库查询/去分页/分类准确）+ 新增场景（is_anime 标记/白名单过滤/混合库归全部/缺省类型全部/剧集状态过滤）均有对应任务（Task 1/2/4/6/7）
- **tasks.md 覆盖**：1.1→T1、1.2→T1、2.1→T2、2.2→T2、2.3→T3、3.1→T4、3.2→T5、3.3→T6、3.4→T6、3.5→T7、4.1→T8，无遗漏
- **占位符扫描**：无 TBD/TODO；每个代码步骤含完整代码
- **类型一致性**：`EmbyLibraryQuery.library_id`（T4 定义）→ T5/T6 使用一致；`list_library_folders` 签名 T1 定义 → T2 引用一致；`EmbyLibraryFolder.id/is_anime`（T4）→ T5/T6 使用一致
