---
change: emby-library-all-poster
design-doc: docs/superpowers/specs/2026-09-11-emby-library-all-poster-design.md
base-ref: 926957f91c6937bc17fd3fb333079f06c2445ff0
---

# Emby「全部」聚合浏览修复 + 封面代理加载 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端新增全部聚合端点修复 Emby「全部」Tab 空列表，Emby 封面改为后端代理加载消除 api_key 暴露与直连并发回源。

**Architecture:** 后端 `services/emby.py` 抽公共参数构造 `_build_library_params`，新增 `list_all_library` 并发聚合全部影视类库并按 emby_id 去重，暴露 `GET /api/emby/library/all`；`services/poster.py` 代理放行 `/emby/<itemId>/Primary` 前缀并回源 Emby 图片；`_normalize_library_item` 的 `poster_url` 改为代理地址。前端全部 Tab 聚合态改单次请求新端点，分类 Tab 与单库下钻行为不变。

**Tech Stack:** FastAPI（Python/httpx/asyncio）、Vue 3 + Pinia + Vitest、pytest（sqlite+aiosqlite）

**Spec:** `docs/openspec/changes/emby-library-all-poster/specs/`（emby-library-browse / poster-proxy）；深度设计见 `docs/superpowers/specs/2026-09-11-emby-library-all-poster-design.md`

## Global Constraints

- 语言：代码注释/提交信息用中文；commit message 用 Conventional Commits（`feat(scope): 中文摘要`）
- 契约：`GET /api/emby/library`（library_id 必选）契约不得改变；新端点独立路径 `/api/emby/library/all`
- 响应契约：`{items, total, item_type}`，item_type 回显
- 错误契约：503 detail 含 `code`，取值 `emby_not_configured` / `emby_unreachable`（与前端 `parseEmbyErrorCode` 对齐）
- 封面代理：poster_url 不再含 api_key；代理校验仅放行受控标识（防 SSRF）；TTL 600s / Cache-Control 86400 沿用
- 后端测试：`pytest`；前端测试：`vitest`；集成：`npm run build`（仓库根或 frontend 目录按现有约定）
- 不做范围外改动：分类 Tab、单库下钻、动漫判定、设置页白名单逻辑保持现状

---

### Task 1: 后端公共参数构造 `_build_library_params`（重构，回归保障）

**Files:**
- Modify: `backend/app/services/emby.py`（新增 `_build_library_params`；`list_library` 改为调用，行为不变）
- Test: `backend/tests/test_emby_library_folders.py`（追加参数构造单测）

**Interfaces:**
- Produces: `_build_library_params(item_type: Optional[str], status: Optional[str], parent_id: Optional[str] = None) -> dict[str, Any]` — 返回含 `Recursive/IncludeItemTypes/Fields/Limit`（+ 可选 `ParentId`/`SeriesStatus`）的 `/Items` 查询参数

- [x] **Step 1: 写失败测试**（追加到 `backend/tests/test_emby_library_folders.py` 末尾）

```python
# ---- _build_library_params（全部聚合与单库共用参数口径）----

def test_build_params_default_types():
    params = emby_mod._build_library_params(None, None)
    assert params["IncludeItemTypes"] == "Movie,Series"
    assert "ParentId" not in params
    assert "SeriesStatus" not in params


def test_build_params_maps_item_type():
    assert emby_mod._build_library_params("movie", None)["IncludeItemTypes"] == "Movie"
    assert emby_mod._build_library_params("series", None)["IncludeItemTypes"] == "Series"


def test_build_params_status_forces_series():
    params = emby_mod._build_library_params("movie", "continuing", parent_id="m1")
    assert params["IncludeItemTypes"] == "Movie,Series"  # status 非空强制含 Series
    assert params["SeriesStatus"] == "continuing"
    assert params["ParentId"] == "m1"
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -k build_params -v`
Expected: FAIL（`AttributeError: module 'app.services.emby' has no attribute '_build_library_params'`）

- [x] **Step 3: 实现 `_build_library_params` 并重构 `list_library`**

在 `backend/app/services/emby.py` 的 `list_library` 上方新增：

```python
def _build_library_params(
    item_type: Optional[str],
    status: Optional[str],
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造 /Items 查询参数：IncludeItemTypes 映射 + SeriesStatus + 分页 + 可选 ParentId。

    单库（list_library）与全部聚合（list_all_library）共用，保证口径一致。
    """
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

将 `list_library` 中 594-606 行的内联参数构造替换为：

```python
    params = _build_library_params(item_type, status, parent_id=library_id)
```

（删除原 `include_item_types`/`params` 内联构造块，其余逻辑不动。）

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -v`
Expected: PASS（新增 3 条 + 既有 10 条全绿，确认重构无回归）

- [x] **Step 5: 提交**

```bash
git add backend/app/services/emby.py backend/tests/test_emby_library_folders.py
git commit -m "refactor(emby): 抽取 _build_library_params 统一 /Items 参数口径"
```

---

### Task 2: 后端 `list_all_library` 聚合服务

**Files:**
- Modify: `backend/app/services/emby.py`（新增 `_LIBRARY_FETCH_CONCURRENCY` 与 `list_all_library`）
- Test: `backend/tests/test_emby_library_all.py`（新增）

**Interfaces:**
- Consumes: `list_library_folders()`（已存在）、`_build_library_params`（Task 1）、`_fetch_items`（已存在）、`_normalize_library_item`（已存在，Task 5 将改 poster_url）、`_attach_tmdb_series_status` / `_attach_in_media_flag`（已存在）
- Produces: `list_all_library(item_type: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]` — 全部影视类库聚合去重结果；配置缺失抛 `EmbyUnavailable`；全部库失败抛 `EmbyUnavailable`；部分失败返回成功部分

- [x] **Step 1: 写失败测试**（新建 `backend/tests/test_emby_library_all.py`）

```python
"""list_all_library（全部影视类库聚合 / GET /api/emby/library/all 服务层）测试。

mock 契约：/Users → 首用户；/Users/user1/Views → 影视类库；/System/Info/Public → serverId；
/Items → 按 params["ParentId"] 分发各库条目（对齐 test_emby_library_folders 既有模式）。
"""
import asyncio
from typing import Any, Callable, Optional

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

import app.services.emby as emby_mod
from app.database import Base
from app.services.emby import EmbyUnavailable, list_all_library


def run(coro):
    return asyncio.run(coro)


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
            await conn.run_sync(Base.metadata.create_all)

    run(_create())
    yield maker
    run(engine.dispose())


def _use_test_db(monkeypatch, maker):
    monkeypatch.setattr(emby_mod, "async_session", maker)


def _install_get(monkeypatch: pytest.MonkeyPatch, handler: Callable):
    async def _fake_get(path: str, params: dict[str, Any], **kwargs):
        result = handler(path, params)
        if asyncio.iscoroutine(result):
            return await result
        return result
    monkeypatch.setattr(emby_mod, "_get", _fake_get)


def _set_cache(monkeypatch):
    """注入 config_store._cache（emby_base_url/emby_api_key）防全量套件残留误判（既有约定）。"""
    monkeypatch.setattr(emby_mod.config_store, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })


def _item(name, kind="Series", tmdb=1001):
    return {
        "Id": f"id-{name}",
        "Name": name,
        "Type": kind,
        "ProductionYear": 2024,
        "ProviderIds": {"Tmdb": str(tmdb)},
        "ImageTags": {"Primary": "x"},
        "SeriesStatus": None,
    }


def _handler(items_by_lib: dict[str, list[dict]], folders: Optional[list[dict]] = None):
    """按库分发 /Items 的桩；folders 缺省为 movies/tvshows/mixed 三类各一个。"""
    if folders is None:
        folders = [
            {"Id": "m1", "Name": "电影", "CollectionType": "movies"},
            {"Id": "t1", "Name": "剧集", "CollectionType": "tvshows"},
            {"Id": "x1", "Name": "混合", "CollectionType": "mixed"},
        ]

    def _h(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": folders}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            return {"Items": items_by_lib.get(params.get("ParentId"), [])}
        raise AssertionError(f"unexpected path: {path}")
    return _h


def test_all_aggregates_movies_tvshows_mixed(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({
        "m1": [_item("M", "Movie", 11)],
        "t1": [_item("T", "Series", 22)],
        "x1": [_item("X", "Movie", 33)],
    }))
    result = run(list_all_library())
    ids = sorted(i["emby_id"] for i in result)
    assert ids == ["id-M", "id-T", "id-X"]  # movies/tvshows/mixed 三类库条目全部聚合


def test_all_dedupes_by_emby_id(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({
        "m1": [_item("dup", "Movie", 11)],
        "t1": [_item("dup", "Series", 11)],  # 同 emby_id 跨库 → 去重保留先到者
    }))
    result = run(list_all_library())
    assert len(result) == 1
    assert result[0]["emby_id"] == "id-dup"


def test_all_no_libraries_returns_empty(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)
    _install_get(monkeypatch, _handler({}, folders=[]))
    assert run(list_all_library()) == []


def test_all_partial_failure_keeps_success(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    def _h(path, params):
        if path == "/Users":
            return [{"Id": "user1", "Name": "admin"}]
        if path == "/Users/user1/Views":
            return {"Items": [{"Id": "m1", "Name": "电影", "CollectionType": "movies"},
                              {"Id": "t1", "Name": "剧集", "CollectionType": "tvshows"}]}
        if path == "/System/Info/Public":
            return {"Id": "srv1"}
        if path == "/Items":
            if params.get("ParentId") == "t1":
                raise EmbyUnavailable("Emby 请求失败: boom")
            return {"Items": [_item("M", "Movie", 11)]}
        raise AssertionError(f"unexpected path: {path}")

    _install_get(monkeypatch, _h)
    result = run(list_all_library())  # 一个库失败 → 不抛，返回成功部分
    assert [i["emby_id"] for i in result] == ["id-M"]


def test_all_all_failed_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_all_library())


def test_all_not_configured_raises(monkeypatch, _db_maker):
    _use_test_db(monkeypatch, _db_maker)
    _set_cache(monkeypatch)

    async def _boom(path, params):
        raise EmbyUnavailable("EMBY_API_KEY 未配置")

    _install_get(monkeypatch, _boom)
    with pytest.raises(EmbyUnavailable):
        run(list_all_library())
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_all.py -v`
Expected: FAIL（`ImportError: cannot import name 'list_all_library'`）

- [x] **Step 3: 实现 `list_all_library`**

在 `backend/app/services/emby.py` 中 `list_library` 之后新增模块级常量与函数：

```python
# 全部聚合逐库并发上限：Emby 单实例，并发过高无收益且可能打爆服务端（低于 TMDB 批处理 5）
_LIBRARY_FETCH_CONCURRENCY = 3


async def list_all_library(
    item_type: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """聚合全部影视类媒体库条目（GET /api/emby/library/all）。

    1. 复用 list_library_folders() 取全部影视类库（movies/tvshows/mixed/null，
       tvshows 白名单过滤沿用）；配置缺失抛 EmbyUnavailable（emby_not_configured）；
       无影视库 → 返回 []。
    2. 逐库并发 _fetch_items（信号量 _LIBRARY_FETCH_CONCURRENCY 限并发）。
    3. 归一化 + 按 emby_id 去重（保留先到者；Emby ItemId 全局唯一，去重仅防御）。
    4. 复用 _attach_tmdb_series_status + _attach_in_media_flag。
    5. 错误语义：全部库失败 → 抛 EmbyUnavailable（emby_unreachable，取首个失败原因）；
       部分失败 → 返回成功部分 + warn 日志。
    """
    folders = await list_library_folders()
    if not folders:
        return []

    sem = asyncio.Semaphore(_LIBRARY_FETCH_CONCURRENCY)

    async def _fetch_one(folder: dict[str, Any]) -> list[dict[str, Any]]:
        async with sem:
            params = _build_library_params(item_type, status, parent_id=folder["id"])
            return await _fetch_items(params)

    errors: list[Exception] = []
    results = await asyncio.gather(
        *(_fetch_one(f) for f in folders), return_exceptions=True
    )
    for exc in results:
        if isinstance(exc, Exception):
            errors.append(exc)

    base = _base_url()
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY)
    seen: dict[str, dict[str, Any]] = {}
    for chunk in results:
        if isinstance(chunk, Exception):
            continue
        for raw in chunk:
            # 注意：当前 _normalize_library_item 为 4 参签名（item/base/api_key/server_id）；
            # Task 5 改为 3 参（删除 api_key）后，此处同步改为 _normalize_library_item(raw, base, server_id=None)
            normalized = _normalize_library_item(raw, base, api_key, server_id=None)
            if normalized is not None:
                seen.setdefault(normalized["emby_id"], normalized)

    # D-1：serverId 批量获取（惰性缓存），一次性为条目附加 emby_web_url
    server_id = await _get_server_id()
    items = list(seen.values())
    if server_id:
        base = _base_url()
        for item in items:
            item["emby_web_url"] = f"{base}/web/index.html#!/item?id={item['emby_id']}&serverId={server_id}"

    await _attach_tmdb_series_status(items)
    await _attach_in_media_flag(items)

    if errors and not items:
        raise errors[0]  # 全部库失败 → 上抛（路由映射 emby_unreachable）
    if errors:
        logger.warning("Emby 全部聚合部分库失败: %d/%d", len(errors), len(folders))
    logger.info("Emby 全部影视库聚合: %d 条（去重后）", len(items))
    return items
```

> 说明：上方案中 `_normalize_library_item` 先以 `server_id=None` 归一化（poster_url/基础字段），随后统一附加 `emby_web_url`，避免逐条触发 `_get_server_id` 的重复逻辑；`list_library` 既有「一次获取批量复用」语义保持不变（其内部仍传 server_id）。

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_all.py -v`
Expected: PASS（6 条全绿）

- [x] **Step 5: 提交**

```bash
git add backend/app/services/emby.py backend/tests/test_emby_library_all.py
git commit -m "feat(emby): 新增 list_all_library 全部影视类库聚合服务"
```

---

### Task 3: 后端端点 `GET /api/emby/library/all`

**Files:**
- Modify: `backend/app/routers/emby.py`（新增 `library_all` 端点与 import）
- Test: `backend/tests/test_emby_library_all.py`（追加端点级测试）

**Interfaces:**
- Consumes: `list_all_library`（Task 2）、`_to_http_exc`（已存在）
- Produces: `GET /api/emby/library/all?item_type=&status=` → `{items, total, item_type}`；503 detail 含 code

- [x] **Step 1: 写失败测试**（追加到 `backend/tests/test_emby_library_all.py` 末尾）

```python
# ---- 端点级（路由直调，对齐 test_poster_proxy 约定；鉴权由 Depends 注入，不经鉴权测试）----


def test_router_library_all_contract(monkeypatch):
    from types import SimpleNamespace
    from app.routers import emby as emby_router

    async def _fake(item_type=None, status=None):
        return [{"emby_id": "x", "title": "X"}]

    monkeypatch.setattr(emby_router, "list_all_library", _fake)
    _user = SimpleNamespace(id=1, role="user", username="u")
    resp = run(emby_router.library_all(item_type=None, status=None, user=_user))
    assert resp == {"items": [{"emby_id": "x", "title": "X"}], "total": 1, "item_type": None}


def test_router_library_all_item_type_echo(monkeypatch):
    from types import SimpleNamespace
    from app.routers import emby as emby_router

    async def _fake(item_type=None, status=None):
        return []

    monkeypatch.setattr(emby_router, "list_all_library", _fake)
    _user = SimpleNamespace(id=1, role="user", username="u")
    resp = run(emby_router.library_all(item_type="movie", status="continuing", user=_user))
    assert resp["item_type"] == "movie"


def test_router_library_all_not_configured_503(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.routers import emby as emby_router

    async def _fake(item_type=None, status=None):
        raise EmbyUnavailable("EMBY_API_KEY 未配置")

    monkeypatch.setattr(emby_router, "list_all_library", _fake)
    _user = SimpleNamespace(id=1, role="user", username="u")
    with pytest.raises(HTTPException) as exc_info:
        run(emby_router.library_all(item_type=None, status=None, user=_user))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "emby_not_configured"


def test_router_library_all_unreachable_503(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.routers import emby as emby_router

    async def _fake(item_type=None, status=None):
        raise EmbyUnavailable("Emby 请求失败: connection refused")

    monkeypatch.setattr(emby_router, "list_all_library", _fake)
    _user = SimpleNamespace(id=1, role="user", username="u")
    with pytest.raises(HTTPException) as exc_info:
        run(emby_router.library_all(item_type=None, status=None, user=_user))
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "emby_unreachable"
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_all.py -k router_library_all -v`
Expected: FAIL（`AttributeError: module 'app.routers.emby' has no attribute 'library_all'`）

- [x] **Step 3: 实现端点**

修改 `backend/app/routers/emby.py`：

```python
from app.services.emby import EmbyUnavailable, list_all_library, list_library, list_library_folders
```

新增端点（放在 `/library` 之后）：

```python
@router.get("/library/all")
async def library_all(
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    user: User = Depends(get_current_user),  # 登录用户可调
) -> dict:
    """全部影视类库聚合查询：一次请求遍历全部影视类库，按 emby_id 去重返回。

    参数/响应/错误契约与 /library 一致（item_type/status 可选；item_type 回显）。
    """
    try:
        items = await list_all_library(item_type, status)
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "items": items,
        "total": len(items),
        "item_type": item_type,
    }
```

> 注意：FastAPI 路由匹配按声明顺序与具体度，`/library/all` 与 `/library` 路径不同不冲突；`/library` 端点保持 `library_id` 必选不变。

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_all.py -v`
Expected: PASS（端点 4 条 + 服务层 6 条全绿）

- [x] **Step 5: 提交**

```bash
git add backend/app/routers/emby.py backend/tests/test_emby_library_all.py
git commit -m "feat(emby): 新增 GET /api/emby/library/all 全部聚合端点"
```

---

### Task 4: 后端封面代理扩展（emby 前缀）

**Files:**
- Modify: `backend/app/services/poster.py`（`_validate_poster_path` emby 分支、emby 回源配置读取、`fetch_poster` 前缀分支）
- Test: `backend/tests/test_poster_proxy.py`（追加 emby 分支测试）

**Interfaces:**
- Consumes: `config_store`（已存在）、`settings`（已存在）、`httpx`（已存在）
- Produces: `_validate_poster_path("/emby/<itemId>/Primary")` 放行；`fetch_poster("/emby/<itemId>/Primary")` 回源 Emby 图片；未配置抛 `PosterUnavailable`

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_poster_proxy.py` 末尾）

```python
# ---- Task 4：Emby 封面代理（/emby/<itemId>/Primary 前缀）----


def test_validate_emby_path_ok():
    assert poster_mod._validate_poster_path("/emby/abc-123/Primary") is True
    assert poster_mod._validate_poster_path("/emby/9f2c1a4b-0000-4c3e-8f7d-1234567890ab/Primary") is True


def test_validate_emby_path_rejects():
    # 非法字符 / 路径穿越 / 协议段 / 多余路径段 / 空 id / 非 Primary 后缀
    for bad in [
        "/emby/../x/Primary",
        "/emby/a/b/Primary",        # 多路径段
        "/emby/a/Primary/x",        # 多余后缀
        "/emby/a/Primary/../x",
        "/emby/",                    # 空 id
        "/emby/abc/Backdrop",        # 非 Primary
        "/emby/ab cd/Primary",       # 空白
        "/emby/ab;cd/Primary",       # 分号
        "/emby/ab%2Fcd/Primary",     # 编码斜杠（FastAPI 已解码一次 → %2F → /）
        "emby/a/Primary",            # 缺前导斜杠
        "/emby//Primary",            # 空段
    ]:
        assert poster_mod._validate_poster_path(bad) is False, bad


def test_fetch_poster_emby_success(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    monkeypatch.setattr(poster_mod, "_ALERT_COOLDOWN", {})
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"emby_base_url": "http://emby.test", "emby_api_key": "k123"},
    )
    content, ctype = asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))
    assert content == b"\xff\xd8jpg"
    assert ctype == "image/jpeg"
    assert calls == ["http://emby.test/Items/abc-123/Images/Primary?api_key=k123"]


def test_fetch_poster_emby_not_configured(monkeypatch):
    monkeypatch.setattr("app.services.config_store._cache", {})
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    with pytest.raises(poster_mod.PosterUnavailable):
        asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))


def test_fetch_poster_emby_cache_isolated_from_tmdb(monkeypatch):
    """/emby/ 与 /t/p/ 缓存 key 天然隔离：emby 命中不回源、tmdb 不误伤。"""
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/emby/abc-123/Primary": (time.monotonic() + 100, "image/jpeg", b"emby-cached")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"tmdb-fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/emby/abc-123/Primary"))
    assert content == b"emby-cached"
    assert calls == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_poster_proxy.py -k emby -v`
Expected: FAIL（`/emby/...` 校验拒绝或回源失败）

- [ ] **Step 3: 实现代理扩展**

修改 `backend/app/services/poster.py`：

顶部 import 增加 `re`：

```python
import re
```

模块级常量（`_POSTER_CACHE_MAX` 附近）：

```python
# Emby ItemId 白名单（GUID 或数字串）；回源 URL 仅由「配置 base + 固定路径 + 白名单 id」拼接
_EMBY_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
```

`_validate_poster_path` 末尾（`/t/p/` 分支之后）追加 emby 分支：

```python
def _validate_poster_path(p: str) -> bool:
    """校验代理相对路径合法性（防 SSRF / 路径穿越）。

    - /t/p/...   ：TMDB 图床相对路径（原有）
    - /emby/<itemId>/Primary：Emby 封面（itemId 白名单字符集，回源地址由配置拼接）
    """
    if not p or not p.strip():
        return False
    if not p.startswith("/"):
        return False
    if "://" in p.lower():
        return False
    if "\\" in p or "\x00" in p:
        return False
    if p.startswith("/emby/"):
        parts = p.split("/")
        if len(parts) != 4 or parts[-1] != "Primary":
            return False
        return bool(_EMBY_ITEM_ID_RE.match(parts[2]))
    norm = posixpath.normpath(p)
    if not norm.startswith("/t/p/"):
        return False
    if norm == "/t/p" or norm == "/t/p/.." or norm.startswith("/t/p/../"):
        return False
    return True
```

新增 emby 配置读取（`_base_url` 之后）：

```python
def _emby_image_url(item_id: str) -> str:
    """构造 Emby 封面回源 URL（配置 base + 固定路径 + 白名单 id + api_key）。

    未配置 → PosterUnavailable（路由映射 503）；误填防御与 _base_url 同规则。
    """
    base = (config_store.get("emby_base_url", settings.EMBY_BASE_URL) or "").strip().rstrip("/")
    api_key = config_store.get("emby_api_key", settings.EMBY_API_KEY) or ""
    if not base or not api_key:
        raise PosterUnavailable("Emby 图片代理未配置（emby_base_url / emby_api_key）")
    if "://" not in base and ":" in base:
        raise PosterUnavailable(
            "Emby 地址疑似填了代理端口，应为 Emby 服务根地址（如 http://192.168.1.10:8096）"
        )
    return f"{base}/Items/{item_id}/Images/Primary?api_key={api_key}"
```

`fetch_poster` 回源 URL 构造改为按前缀分支：

```python
    if p.startswith("/emby/"):
        url = _emby_image_url(p.split("/")[2])
    else:
        url = f"{_base_url()}{p}"
```

（其余逻辑不变：缓存 key 即 p 原文，前缀天然隔离；失败路径不写缓存。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_poster_proxy.py -v`
Expected: PASS（新增 5 条 + 既有 13 条全绿；`_validate_poster_path` 改动不回归 `/t/p/` 校验）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/poster.py backend/tests/test_poster_proxy.py
git commit -m "feat(poster): 代理扩展支持 Emby 封面回源（/emby/<itemId>/Primary）"
```

---

### Task 5: 后端 `_normalize_library_item` poster_url 改造

**Files:**
- Modify: `backend/app/services/emby.py`（`_normalize_library_item` poster_url 代理格式、签名去掉 api_key；`list_library` 调用点同步）
- Test: `backend/tests/test_emby_library_folders.py`（追加 poster_url 断言）

**Interfaces:**
- Consumes: 无新依赖
- Produces: `_normalize_library_item(item, base, server_id=None)`（api_key 参数删除）；poster_url 为 `/api/poster?p=emby/{item_id}/Primary`

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_emby_library_folders.py` 末尾）

```python
# ---- _normalize_library_item poster_url 代理格式（Task 5）----


def test_normalize_poster_url_proxy_format():
    item = _library_item("A", kind="Movie", tmdb=11)
    result = emby_mod._normalize_library_item(item, "http://emby.test", server_id="srv1")
    assert result["poster_url"] == "/api/poster?p=emby/id-A/Primary"
    assert "api_key" not in result["poster_url"]  # api_key 不再内嵌
    assert result["emby_web_url"] == (
        "http://emby.test/web/index.html#!/item?id=id-A&serverId=srv1"
    )


def test_normalize_no_poster_returns_null():
    item = {
        "Id": "id-N",
        "Name": "N",
        "Type": "Movie",
        "ProviderIds": {"Tmdb": "11"},
        "ImageTags": {},
    }
    result = emby_mod._normalize_library_item(item, "http://emby.test", server_id="srv1")
    assert result["poster_url"] is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_emby_library_folders.py -k normalize -v`
Expected: FAIL（当前 poster_url 为 Emby 直连 URL）

- [ ] **Step 3: 实现改造**

修改 `backend/app/services/emby.py` `_normalize_library_item`：

签名与 poster_url 生成（378 行附近）：

```python
def _normalize_library_item(
    item: dict[str, Any], base: str, server_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
```

```python
    poster_url = None
    if has_poster:
        # Emby 封面改经后端代理加载：不内嵌 api_key，前端同源请求（登录态 cookie 兜底）
        poster_url = f"/api/poster?p=emby/{item_id}/Primary"
```

同步修改调用点 `list_library`（616 行）：

```python
        normalized = _normalize_library_item(item, base, server_id)
```

（删除 `api_key` 局部变量——`_base_url()` 与 `api_key` 获取行中 api_key 不再需要；保留 `base`。）

> 注意：`list_library` 中 `api_key = config_store.get(...)` 行删除后，确认无其他引用；`base` 仍用于 emby_web_url。**必须同步修改两个调用点**（缺一会 TypeError）：
> 1. `list_library`（原 616 行）：`_normalize_library_item(item, base, api_key, server_id)` → `_normalize_library_item(item, base, server_id)`
> 2. `list_all_library`（Task 2 新增）：`_normalize_library_item(raw, base, api_key, server_id=None)` → `_normalize_library_item(raw, base, server_id=None)`，并删除其 `api_key` 局部变量

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_emby_library_all.py tests/test_emby_library_folders.py tests/test_emby_series_status.py -v`
Expected: PASS（新旧用例全绿，含 Task 2 聚合测试——其断言不含 poster_url 具体值，不受影响）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/emby.py backend/tests/test_emby_library_folders.py
git commit -m "feat(emby): Emby 条目 poster_url 改为后端代理地址，去除 api_key 内嵌"
```

---

### Task 6: 前端 API 函数与全部 Tab 单次请求

**Files:**
- Modify: `frontend/src/api/index.ts`（新增 `listAllEmbyLibraryApi`）
- Modify: `frontend/src/views/EmbyLibraryView.vue`（`fetchCurrent` 全部聚合态分流）
- Test: `frontend/src/views/embyLibraryView.test.ts`（更新聚合测试为单次请求、补全部 Tab 用例）

**Interfaces:**
- Consumes: `http`（已存在）、`EmbyLibraryResponse`/`EmbyItemType`/`EmbySeriesStatus` 类型（已存在）
- Produces: `listAllEmbyLibraryApi(params: { itemType?: EmbyItemType; status?: EmbySeriesStatus }) -> Promise<EmbyLibraryResponse>`

- [ ] **Step 1: 写失败测试**（更新 `frontend/src/views/embyLibraryView.test.ts`）

在 `vi.mock('../api', ...)` 中追加 `listAllEmbyLibraryApi: vi.fn()`，并在 import 中引入：

```typescript
import { listEmbyLibrariesApi, listEmbyLibraryApi, listAllEmbyLibraryApi } from '../api'
```

```typescript
const mockedListAll = vi.mocked(listAllEmbyLibraryApi)
```

将「多库聚合：按 emby_id 去重合并，全量渲染不截断（无分页）」用例替换为全部 Tab 单次请求用例：

```typescript
it('全部 Tab 聚合态：单次调用 listAllEmbyLibraryApi（不逐库请求）', async () => {
  const libs = [makeLib('m1', 'movies'), makeLib('t1', 'tvshows'), makeLib('x1', 'mixed')]
  mockedList.mockResolvedValue({ libraries: libs, total: 3 })

  const items = Array.from({ length: 120 }, (_, i) => makeItem(`a-${i}`, `片 A${i}`))
  mockedListAll.mockResolvedValue({ items, total: items.length, item_type: null })

  wrapper = mountView()
  await flushPromises()

  expect(mockedListAll).toHaveBeenCalledTimes(1)
  expect(mockedListAll).toHaveBeenCalledWith({ status: undefined })
  expect(mockedListLibrary).not.toHaveBeenCalled() // 全部 Tab 不再逐库
  const store = useEmbyStore()
  expect(store.items).toHaveLength(120)
  expect(store.error).toBeNull()
  expect(wrapper.findAll('.lc-media-card')).toHaveLength(120)
})

it('全部 Tab 聚合失败：置错误态', async () => {
  const libs = [makeLib('m1', 'movies')]
  mockedList.mockResolvedValue({ libraries: libs, total: 1 })
  mockedListAll.mockRejectedValue({
    response: { data: { detail: { code: 'emby_unreachable' } } },
  })

  wrapper = mountView()
  await flushPromises()

  expect(useEmbyStore().error).toBe('unavailable')
})
```

> 「单库下钻」用例保留（下钻仍走 `store.fetchLibrary`/`listEmbyLibraryApi`，不受影响）。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/views/embyLibraryView.test.ts`
Expected: FAIL（视图仍逐库请求，`mockedListAll` 未调用）

- [ ] **Step 3: 实现前端改造**

`frontend/src/api/index.ts`（`listEmbyLibraryApi` 之后新增）：

```typescript
/**
 * 全部影视类库聚合查询（GET /api/emby/library/all）。
 * item_type/status 可选，契约与单库端点一致（item_type 回显；错误码同 503 detail.code）。
 */
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

（确认文件已 import `EmbyItemType`/`EmbySeriesStatus` 类型；无则补。）

`frontend/src/views/EmbyLibraryView.vue`：

- import 增加 `listAllEmbyLibraryApi`
- `fetchCurrent` 在「下钻判断」之后、`libs.length === 0` 之前插入全部聚合态分支：

```typescript
  if (category.value === 'all') {
    // 全部聚合态：后端聚合端点单次请求（多库去重由服务端保证）
    store.loading = true
    try {
      const res = await listAllEmbyLibraryApi({ status: statusFilter.value || undefined })
      store.items = res.items
      store.error = null
    } catch (err) {
      store.items = []
      store.error = parseEmbyErrorCode(err)
    } finally {
      store.loading = false
    }
    return
  }
```

> 其余（movie/series/anime 聚合逐库、下钻 fetchSingle、posterErrors 兜底）保持不变。封面 `<img :src="item.poster_url">` 直接使用后端代理地址，无需改动。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/views/embyLibraryView.test.ts`
Expected: PASS（全部 Tab 单次请求 + 失败错误态 + 单库下钻用例全绿）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/api/index.ts frontend/src/views/EmbyLibraryView.vue frontend/src/views/embyLibraryView.test.ts
git commit -m "feat(frontend): 全部 Tab 改走 /emby/library/all 单次聚合请求"
```

---

### Task 7: 集成验证与真实环境诊断

**Files:**
- 无代码改动（验证产物：tasks.md 勾选、诊断结论）

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest -v`
Expected: PASS（全量用例绿；若命中无关历史失败，记录并报告，不静默跳过）

- [ ] **Step 2: 前端测试与构建**

Run: `cd frontend && npx vitest run && npm run build`
Expected: PASS（vitest 全绿；`npm run build` 产物生成成功）

- [ ] **Step 3: 真实 Emby 环境诊断（需用户环境，记录诊断结论到 tasks.md 1.1/4.1）**

- 确认 `GET /api/emby/library/all` 对真实环境返回非空且按 emby_id 去重
- 确认封面经 `/api/poster?p=emby/<id>/Primary` 加载、页面无 Emby 直连请求、无 api_key 泄露
- 记录 `library_id=7`（或对应真实库）空列表的根因诊断结论（前端分组缺陷已由聚合端点绕过；如后端单库查询另有问题则记录）
- 用户确认后勾选 tasks.md 的 4.1（真实环境验证项；未连接环境则标注「待用户环境验证」）

- [ ] **Step 4: 勾选 tasks.md 全部任务并提交验证证据**

按实际完成情况勾选 `docs/openspec/changes/emby-library-all-poster/tasks.md` 全部 10 项，提交：

```bash
git add docs/openspec/changes/emby-library-all-poster/tasks.md
git commit -m "chore(emby): emby-library-all-poster 全部任务完成并勾选"
```
