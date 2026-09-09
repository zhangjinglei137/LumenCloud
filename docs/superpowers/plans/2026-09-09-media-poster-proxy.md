---
change: media-poster-proxy
design-doc: docs/superpowers/specs/2026-09-09-media-poster-proxy-design.md
base-ref: 11d5051725a57ab858eee276c86c22d92e249c34
---

# 影视海报图床代理（media-poster-proxy）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端新增 `GET /api/poster` 海报代理端点（含路径校验/镜像配置/缓存/鉴权），前端所有海报展示统一经 `posterUrl()` 走后端代理，解决墙内直连 TMDB 图床不可达导致的海报不显示问题。

**Architecture:**
- 后端：新增 `services/poster.py`（路径校验 + 镜像基址 + httpx 回源 + 内存 TTL 缓存 + 节流告警）与 `routers/poster.py`（端点 + `get_current_user` 鉴权 + 错误映射 400/401/502/503），挂入 `routers/api.py` 的 `/api` 前缀。
- 配置：`config.py` 新增 `TMDB_POSTER_PROXY`，`settings.py` PATCH 白名单新增 `tmdb_poster_proxy`（config_store 自动 DB 优先、env 兜底、保存即生效）。
- 前端：新增 `utils/poster.ts` 的 `posterUrl(path)` → `/api/poster?p=${encodeURIComponent(path)}`，替换 5 个视图/组件的全部 `TMDB_POSTER_BASE` 拼接点，删除 `types/index.ts` 中已无引用的 `TMDB_POSTER_BASE` 常量；新增 vitest 单测基建。

**Tech Stack:** Python 3.11+ / FastAPI / httpx 0.28 / SQLAlchemy async / pytest；Vue 3 / TypeScript / Vite 6 / vitest。

**Spec:**
- `docs/openspec/changes/media-poster-proxy/specs/poster-proxy/spec.md`
- 深度设计：`docs/superpowers/specs/2026-09-09-media-poster-proxy-design.md`

## Global Constraints

- **产物语言**：所有 commit message 使用中文，遵循 Conventional Commits 结构（`<type>(<scope>): <中文摘要>`），type 取 `feat`/`test`/`chore` 等，摘要 50 字符内、动词开头（如「新增」「修复」）、不写句号。
- **TDD**：每个实现任务先写失败测试（红）→ 运行确认失败 → 实现（绿）→ 运行确认通过 → 提交。禁止跳过失败测试验证阶段。
- **测试隔离**：后端单测不得连真实外部服务；外部调用全部 `monkeypatch.setattr("app.services.poster.httpx.AsyncClient", factory)`（参考 `backend/tests/test_tmdb_cache.py` 范式）。涉及 config_store 时注入 `config_store._cache` dict（参考 `test_emby_series_status.py`）。
- **规范约束**：poster_path 契约不变（仍返回相对路径，不拼接绝对 URL）；不新增数据库表/迁移；不改 Emby 库自身海报（`poster_url` 完整 URL 逻辑不动）。
- **鉴权**：poster 端点必须 `Depends(get_current_user)`（登录可调，cookie 兜底）。未登录 401。
- **后端运行环境**：`backend` 目录下运行 pytest（如 `python -m pytest tests/test_xxx.py -v`）；不依赖 localhost:8000 演练进程，验证以测试套件为准。
- **前端验证**：`npm run build`（= `vue-tsc --noEmit && vite build`）与 `npm test`（vitest）须通过。

## 文件结构映射

**新增**
- `backend/app/services/poster.py`：校验、镜像基址、回源、缓存、节流、错误类型
- `backend/app/routers/poster.py`：端点与错误映射
- `backend/tests/test_poster_path_validation.py`
- `backend/tests/test_poster_proxy.py`（回源/镜像/错误/缓存）
- `backend/tests/test_poster_auth.py`（鉴权 401/200）
- `frontend/src/utils/poster.ts`
- `frontend/src/utils/poster.test.ts`

**修改**
- `backend/app/config.py`（+`TMDB_POSTER_PROXY: str = ""`）
- `backend/app/routers/api.py`（挂载 poster router）
- `backend/app/routers/settings.py`（白名单 +`"tmdb_poster_proxy"`）
- `frontend/package.json` / `frontend/vite.config.ts`（+vitest）
- `frontend/src/types/index.ts`（删除 `TMDB_POSTER_BASE` 常量）
- `frontend/src/views/MediaListView.vue`、`views/MediaDetailView.vue`、`views/MediaAddView.vue`、`views/ApprovalsView.vue`
- `frontend/src/components/TmdbSearch.vue`

**不修改**：`views/EmbyLibraryView.vue`（其卡片用 Emby 完整 `poster_url`，Non-Goal；对话框海报经 TmdbSearch 组件内部替换自动覆盖）

---

### Task 1: 服务层路径校验 + 路由端点骨架（tasks.md 1.1）

**Files:**
- Create: `backend/app/services/poster.py`
- Create: `backend/app/routers/poster.py`
- Modify: `backend/app/routers/api.py`
- Create: `backend/tests/test_poster_path_validation.py`

**Interfaces:**
- Produces:
  - `app.services.poster._validate_poster_path(p: str) -> bool` — 返回路径是否合法（不会被后续任务删除或改签名）
  - `app.services.poster.PosterUnavailable(Exception)` — 配置类错误（路由映射 503）
  - `routers/poster.py`: `router = APIRouter(prefix="/poster", tags=["poster"])`，`@router.get("")` 端点 `get_poster(p, user)`
- Consumes: `app.routers.deps.get_current_user`、`app.models.User`

- [x] **Step 1: 写失败测试 `test_poster_path_validation.py`**

```python
"""海报代理路径校验单测。"""
import pytest

from app.services.poster import _validate_poster_path

VALID = "/t/p/w500/ab12cd.jpg"


@pytest.mark.parametrize("path", [
    VALID,
    "/t/p/w500/x.jpg",
    "/t/p/original/x%20y.jpg",
])
def test_valid_paths(path):
    assert _validate_poster_path(path) is True


@pytest.mark.parametrize("path", [
    "",
    "   ",
    "t/p/w500/x.jpg",          # 不以 / 开头
    "../etc/passwd",
    "/t/p/../../x.jpg",        # 归一化越界
    "/t/p/w500/../..",
    "http://evil.com/x.jpg",
    "https://image.tmdb.org/t/p/w500/x.jpg",  # 完整 URL：协议段由 "://" 拦截
    "//host/t/p/x.jpg",
    "/t/p/x.jpg\\..\\..",
    "/t/p/x\x00.jpg",
    "/t/p",                    # 无子资源路径
    "/t/p/",
    "/t/p/..",
    "%2e%2e%2fetc%2fpasswd",   # 解码后为 ../etc/passwd
])
def test_invalid_paths(path):
    assert _validate_poster_path(path) is False
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_poster_path_validation.py -v`（workdir: `backend`）
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.poster'`

- [x] **Step 3: 实现 `services/poster.py` 校验部分**

```python
"""影视海报图床代理服务。

- 校验：_validate_poster_path 仅放行 /t/p/... 形态（防 SSRF/路径穿越）
- 回源：httpx 拉取镜像/官方图床，返回 bytes + content_type
- 缓存：进程内 TTL dict（上限 + 过期），纯优化，任何异常降级直出
- 节流：模块级 {key: ts}，60s 内同键只告警一次
"""
import logging
import posixpath
import time
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

POSTER_DEFAULT_BASE = "https://image.tmdb.org"
REQUEST_TIMEOUT = httpx.Timeout(10.0)

# 缓存：path → (expire_ts, content_type, bytes)
_POSTER_CACHE_TTL = 600
_POSTER_CACHE_MAX = 100
_POSTER_CACHE: dict[str, tuple[float, str, bytes]] = {}

# 告警节流：path → 上次告警时间戳
_POSTER_ALERT_TTL = 60
_ALERT_COOLDOWN: dict[str, float] = {}


class PosterUnavailable(Exception):
    """海报代理不可用：配置缺失 / 配置误填（防御校验）→ 503。"""


def _validate_poster_path(p: str) -> bool:
    """校验 TMDB 图床相对路径合法性（防 SSRF / 路径穿越）。

    - FastAPI Query 已解码一次 URL 编码，%2e%2e → ..、%2f → /，无需再次解码；
    - posixpath.normpath 消化 ../ 分段后与 /t/p/ 前缀复核，杜绝归一化越界；
    - 协议段（://）与反斜杠、null 字节直接拒绝。
    """
    if not p or not p.strip():
        return False
    if not p.startswith("/"):
        return False
    if "://" in p.lower():
        return False
    if "\\" in p or "\x00" in p:
        return False
    norm = posixpath.normpath(p)
    if not norm.startswith("/t/p/"):
        return False
    if norm == "/t/p" or norm == "/t/p/.." or norm.startswith("/t/p/../"):
        return False
    return True


async def fetch_poster(p: str) -> tuple[bytes, str]:
    """回源拉取海报（Task 2 实现完整逻辑；此处占位保证路由可测试）。"""
    raise NotImplementedError
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_poster_path_validation.py -v`
Expected: PASS

- [x] **Step 5: 写路由端点失败测试（骨架 + 400 + 未实现 502 分支先占位）**

在 `test_poster_proxy.py` 中先只加校验相关的端点行为断言；本步同时创建 `routers/poster.py` 骨架文件（端点存在即可，回源逻辑 Task 2 实现）：

```python
# backend/tests/test_poster_proxy.py（本步先写入校验/Router 相关用例，其余 Task 2 追加）
"""海报代理端点单测（路由函数直调，user 传 fake；鉴权在 test_poster_auth.py）。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.poster as poster_mod
from app.routers import poster as poster_router

_USER = SimpleNamespace(id=1, role="user", username="u")


def test_router_rejects_invalid_path(monkeypatch):
    """端点对非法路径返回 400，且不发起对外请求。"""
    fetch = AsyncMock()
    monkeypatch.setattr(poster_mod, "fetch_poster", fetch)

    async def _run():
        return await poster_router.get_poster(p="/t/p/../../etc/passwd", user=_USER)

    resp = asyncio.run(_run())
    assert resp.status_code == 400
    fetch.assert_not_awaited()
```

对应 `routers/poster.py` 实现（校验先行，回源失败占位在后面任务补充）：

```python
"""影视海报代理 API。

- GET /api/poster?p=<相对路径>：登录用户可调（cookie 兜底，<img> 同源可用）
- 校验非法 → 400；配置误填 → 503；回源失败 → 502；未登录 → 401
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response as FastAPIResponse

from app.models import User
from app.routers.deps import get_current_user
from app.services.poster import PosterUnavailable, _validate_poster_path, fetch_poster

router = APIRouter(prefix="/poster", tags=["poster"])


@router.get("")
async def get_poster(
    p: str = Query(min_length=1, max_length=512),
    user: User = Depends(get_current_user),
):
    """代理拉取 TMDB 图床海报图片。"""
    if not _validate_poster_path(p):
        raise HTTPException(status_code=400, detail="非法海报路径")
    try:
        content, content_type = await fetch_poster(p)
    except PosterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  回源失败统一 502
        raise HTTPException(status_code=502, detail=f"海报代理拉取失败: {exc}") from exc
    return FastAPIResponse(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
```

- [x] **Step 6: 挂载路由到 `api.py`**

`backend/app/routers/api.py`：`_business_router.include_router(poster.router)`（`poster` 加入 import 列表，与 tmdb 相邻）。

- [x] **Step 7: 运行测试确认通过**

Run: `python -m pytest tests/test_poster_path_validation.py tests/test_poster_proxy.py::test_router_rejects_invalid_path -v`
Expected: PASS（`fetch_poster` 占位抛 `NotImplementedError`，但该用例只断言 400 分支，不触达回源）

- [x] **Step 8: Commit**

```bash
git add backend/app/services/poster.py backend/app/routers/poster.py backend/app/routers/api.py backend/tests/test_poster_path_validation.py backend/tests/test_poster_proxy.py
git commit -m "feat(poster): 新增海报代理路径校验与路由骨架"
```

---

### Task 2: 代理回源逻辑（tasks.md 1.2）

**Files:**
- Modify: `backend/app/services/poster.py`（实现 `fetch_poster` + 节流）
- Modify: `backend/tests/test_poster_proxy.py`（追加回源用例）

**Interfaces:**
- Consumes: `_validate_poster_path`、`PosterUnavailable`、`config_store.get("tmdb_poster_proxy", settings.TMDB_POSTER_PROXY)`（Task 3 前该 key 未配置恒为空 → 回退官方）
- Produces: `fetch_poster(p: str) -> tuple[bytes, str]` — 返回 (图片 bytes, content_type)；配置误填抛 `PosterUnavailable`；网络/非 2xx 抛 `Exception`（路由映射 502）

- [x] **Step 1: 写失败测试（回源成功/失败/节流）**

追加到 `tests/test_poster_proxy.py`：

```python
import asyncio
import time

import httpx

# ---- 回源 ----

def _make_client_factory(resp, calls):
    """构造返回 FakeClient 的工厂；FakeClient.get 记录 url 并返回预设 resp。"""
    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            calls.append(url)
            return resp
    return lambda *a, **kw: FakeClient()


def test_fetch_poster_success(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"\xff\xd8jpg", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"\xff\xd8jpg"
    assert ctype == "image/jpeg"
    assert calls == ["https://image.tmdb.org/t/p/w500/x.jpg"]


def test_fetch_poster_upstream_5xx_raises(monkeypatch):
    calls: list[str] = []
    resp = SimpleNamespace(status_code=500, content=b"", headers={})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    with pytest.raises(Exception):
        asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))


def test_fetch_poster_network_error_raises(monkeypatch):
    class BoomClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def get(self, url):
            raise httpx.ConnectError("conn refused")
    monkeypatch.setattr(poster_mod, "_client_factory", lambda *a, **kw: BoomClient())
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    with pytest.raises(Exception):
        asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_poster_proxy.py -v`
Expected: FAIL — `fetch_poster` 抛 `NotImplementedError`

- [x] **Step 3: 实现回源逻辑**

`services/poster.py` 增补：

```python
def _client_factory():
    """httpx.AsyncClient 实例化入口（测试 monkeypatch 挂点）。"""
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT)


def _alert(path: str) -> None:
    """节流告警：60s 内同 path 只 warning 一次。"""
    now = time.monotonic()
    last = _ALERT_COOLDOWN.get(path)
    if last is None or now - last >= _POSTER_ALERT_TTL:
        _ALERT_COOLDOWN[path] = now
        logger.warning("海报代理回源失败 path=%s", path)
    # 清理过期键，防无限增长
    if len(_ALERT_COOLDOWN) > _POSTER_CACHE_MAX * 2:
        for k in [k for k, ts in _ALERT_COOLDOWN.items() if now - ts >= _POSTER_ALERT_TTL]:
            _ALERT_COOLDOWN.pop(k, None)


async def fetch_poster(p: str) -> tuple[bytes, str]:
    """回源拉取海报图片。

    返回 (bytes, content_type)。失败：PosterUnavailable（配置误填/缺失）或
    Exception（网络/非 2xx，路由映射 502）。
    """
    base = _base_url()  # Task 3 实现；先临时内联官方地址
    url = f"{base}{p}"
    try:
        async with _client_factory() as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        _alert(p)
        raise Exception(f"网络请求失败: {exc}") from exc
    if resp.status_code != 200:
        _alert(p)
        raise Exception(f"上游返回 HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "image/jpeg")
    return resp.content, ctype
```

Task 3 之前，`_base_url()` 先放临时实现：

```python
POSTER_DEFAULT_BASE = "https://image.tmdb.org"

def _base_url() -> str:
    return POSTER_DEFAULT_BASE
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_poster_proxy.py tests/test_poster_path_validation.py -v`
Expected: PASS（Test 1 中 test_router_rejects_invalid_path 也通过）

- [x] **Step 5: Commit**

```bash
git add backend/app/services/poster.py backend/tests/test_poster_proxy.py
git commit -m "feat(poster): 实现海报回源拉取与失败节流告警"
```

---

### Task 3: 镜像配置键 + 误填防御 + 镜像/回退切换（tasks.md 2.1 + 2.2）

**Files:**
- Modify: `backend/app/config.py`（+`TMDB_POSTER_PROXY`）
- Modify: `backend/app/routers/settings.py`（白名单 +`"tmdb_poster_proxy"`）
- Modify: `backend/app/services/poster.py`（`_base_url` 镜像解析 + 误填防御）
- Modify: `backend/tests/test_poster_proxy.py`（镜像/回退/防御用例）

**Interfaces:**
- Consumes: `config_store.get(key, default)`、`settings.TMDB_POSTER_PROXY`
- Produces: `_base_url() -> str` — 镜像优先，未配置回退 `POSTER_DEFAULT_BASE`；无 scheme 的 `host:port` 抛 `PosterUnavailable`

- [x] **Step 1: 写失败测试（镜像切换 + 防御校验）**

追加到 `tests/test_poster_proxy.py`（`_base_url` 为同步函数，与 tmdb.py 一致）：

```python
def test_base_url_prefers_mirror(monkeypatch):
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "https://mirror.example.com"},
    )
    assert poster_mod._base_url() == "https://mirror.example.com"


def test_base_url_falls_back_to_official(monkeypatch):
    monkeypatch.setattr("app.services.config_store._cache", {})
    assert poster_mod._base_url() == "https://image.tmdb.org"


def test_base_url_rejects_proxy_port(monkeypatch):
    """误填科学上网代理端口（无 scheme 的 host:port）→ PosterUnavailable。"""
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "192.168.3.31:7897"},
    )
    with pytest.raises(poster_mod.PosterUnavailable):
        poster_mod._base_url()


def test_mirror_path_requests(monkeypatch):
    """配置镜像后回源 URL 使用镜像根地址。"""
    monkeypatch.setattr(
        "app.services.config_store._cache",
        {"tmdb_poster_proxy": "https://mirror.example.com"},
    )
    monkeypatch.setattr(poster_mod, "_POSTER_CACHE", {})
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"img", headers={"content-type": "image/png"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    import asyncio
    asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.png"))
    assert calls == ["https://mirror.example.com/t/p/w500/x.png"]
```

同时写配置键注册的断言测试（settings 白名单 + config.py 字段）追加到 `tests/test_poster_proxy.py`：

```python
def test_settings_whitelist_contains_poster_proxy():
    from app.routers.settings import _WHITELIST_EXACT
    assert "tmdb_poster_proxy" in _WHITELIST_EXACT


def test_config_has_poster_proxy_field():
    from app.config import settings as s
    assert hasattr(s, "TMDB_POSTER_PROXY")
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_poster_proxy.py -v`
Expected: FAIL（5 个新用例）

- [x] **Step 3: 实现配置与镜像解析**

`backend/app/config.py`（`TMDB_API_KEY` 附近）新增：

```python
    # 图床镜像根地址（反代根，如 https://tmdb-image.example.com）：配置后海报
    # 代理从此镜像取图，未配置回退官方 image.tmdb.org。与 TMDB_PROXY（API 镜像）
    # 相互独立。
    TMDB_POSTER_PROXY: str = ""
```

`backend/app/routers/settings.py` `_WHITELIST_EXACT`（tmdb 行）改为：

```python
    "tmdb_api_key", "tmdb_proxy", "tmdb_http_proxy", "tmdb_poster_proxy",
```

`services/poster.py` 替换 `_base_url` 临时实现：

```python
def _base_url() -> str:
    """海报图床根地址：镜像（tmdb_poster_proxy）优先，否则官方 image.tmdb.org。

    误填防御（复用 tmdb.py _base_url 语义）：无 scheme 的 host:port
    （如 192.168.3.31:7897）一定是误填的科学上网代理端口 → 明确报错。
    """
    mirror = (
        config_store.get("tmdb_poster_proxy", settings.TMDB_POSTER_PROXY) or ""
    ).strip().rstrip("/")
    if mirror and "://" not in mirror and ":" in mirror:
        raise PosterUnavailable(
            f"图床镜像地址疑似填了代理端口（{mirror}）。tmdb_poster_proxy 应为图床"
            "反代根地址（如 https://tmdb-image.example.com）；科学上网代理请填到"
            "「TMDB 出口代理」（tmdb_http_proxy）。设置页 → 服务凭据 → 元数据 · TMDB 修改。"
        )
    return mirror or POSTER_DEFAULT_BASE
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_poster_proxy.py tests/test_poster_path_validation.py -v`
Expected: PASS（全部新用例）

- [x] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/routers/settings.py backend/app/services/poster.py backend/tests/test_poster_proxy.py
git commit -m "feat(poster): 新增图床镜像配置与误填防御校验"
```

---

### Task 4: 前端 posterUrl 封装 + vitest 基建（tasks.md 3.1）

**Files:**
- Create: `frontend/src/utils/poster.ts`
- Create: `frontend/src/utils/poster.test.ts`
- Modify: `frontend/package.json`（devDependencies +vitest + npm script）
- Modify: `frontend/vite.config.ts`（+test 配置）

**Interfaces:**
- Produces: `posterUrl(path: string | null | undefined): string | null` — null/空 → null；否则 `/api/poster?p=${encodeURIComponent(path)}`
- Consumes: 无（纯函数，Task 5 的视图 import 它）

- [ ] **Step 1: 写失败测试 `poster.test.ts`**

```ts
import { describe, expect, it } from 'vitest'
import { posterUrl } from './poster'

describe('posterUrl', () => {
  it('null / undefined / 空串返回 null', () => {
    expect(posterUrl(null)).toBeNull()
    expect(posterUrl(undefined)).toBeNull()
    expect(posterUrl('')).toBeNull()
  })

  it('合法路径编码为代理 query 参数', () => {
    expect(posterUrl('/t/p/w500/x.jpg')).toBe('/api/poster?p=%2Ft%2Fp%2Fw500%2Fx.jpg')
  })

  it('特殊字符路径正确编码', () => {
    expect(posterUrl('/t/p/w500/a b?c&d=1')).toBe(
      '/api/poster?p=%2Ft%2Fp%2Fw500%2Fa%20b%3Fc%26d%3D1',
    )
  })
})
```

- [ ] **Step 2: 安装 vitest 并运行确认失败**

```bash
cd frontend && npm install -D vitest
```

`package.json` scripts 增加 `"test": "vitest run"`；`vite.config.ts` 顶部加 `/// <reference types="vitest" />` 并给 defineConfig 加：

```ts
  test: {
    environment: 'node',
  },
```

Run: `npm test`
Expected: FAIL — `Cannot find module './poster'`

- [ ] **Step 3: 实现 `utils/poster.ts`**

```ts
/**
 * TMDB 海报相对路径 → 后端海报代理地址。
 * 空/null 返回 null（调用方已有标题占位兜底）；路径经 encodeURIComponent 编码。
 */
export function posterUrl(path: string | null | undefined): string | null {
  if (!path) return null
  return `/api/poster?p=${encodeURIComponent(path)}`
}
```

- [ ] **Step 4: 运行确认通过**

Run: `npm test`
Expected: PASS（3 用例）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/utils/poster.ts frontend/src/utils/poster.test.ts frontend/package.json frontend/package-lock.json frontend/vite.config.ts
git commit -m "feat(poster): 前端新增 posterUrl 封装与 vitest 单测基建"
```

---

### Task 5: 视图替换全部使用点 + 删除常量 + 构建（tasks.md 3.2 + 3.3）

**Files:**
- Modify: `frontend/src/types/index.ts`（删除 `TMDB_POSTER_BASE`）
- Modify: `frontend/src/components/TmdbSearch.vue`
- Modify: `frontend/src/views/MediaListView.vue`
- Modify: `frontend/src/views/MediaDetailView.vue`
- Modify: `frontend/src/views/MediaAddView.vue`
- Modify: `frontend/src/views/ApprovalsView.vue`

**Interfaces:**
- Consumes: `posterUrl` from `../utils/poster`
- Produces: 无（行为等价替换；`TMDB_POSTER_BASE` 常量删除后全仓无引用）

- [ ] **Step 1: 逐文件替换（先改 import，后改 :src）**

统一模式：`import { TMDB_POSTER_BASE, ... } from '../types'` → 拆分类型 import + 新增 `import { posterUrl } from '../utils/poster'`。

**TmdbSearch.vue**
```diff
- import { TMDB_POSTER_BASE, type TmdbSearchResult } from '../types'
+ import type { TmdbSearchResult } from '../types'
  import { mediaTypeLabel } from '../utils/format'
+ import { posterUrl } from '../utils/poster'
...
  function posterUrl(p: string | null): string | null {
-   return p ? `${TMDB_POSTER_BASE}${p}` : null
+   return posterUrlFromUtils(p)
  }
```
> 说明：组件内已有同名 `posterUrl` 函数，重命名为 `posterSrc` 并委托 utils：

```diff
- function posterUrl(p: string | null): string | null {
-   return p ? `${TMDB_POSTER_BASE}${p}` : null
- }
+function posterSrc(p: string | null): string | null {
+  return posterUrl(p)
+}
```
模板中 `posterUrl(item.poster_path)` 三处引用同步改为 `posterSrc(item.poster_path)`。

**MediaListView.vue**
```diff
- import { TMDB_POSTER_BASE, type MediaItem } from '../types'
+ import type { MediaItem } from '../types'
+ import { posterUrl } from '../utils/poster'
```
`:146` 与 `:207`：`` :src="`${TMDB_POSTER_BASE}${m.poster_path}`" `` → `:src="posterUrl(m.poster_path)"`；表格同 `row`。

**MediaDetailView.vue**
```diff
- import { TMDB_POSTER_BASE } from '../types'
+ import { posterUrl } from '../utils/poster'
```
`:164`：`` :src="`${TMDB_POSTER_BASE}${detail.poster_path}`" `` → `:src="posterUrl(detail.poster_path)"`。

**MediaAddView.vue**
```diff
- import { TMDB_POSTER_BASE, type TmdbSearchResult } from '../types'
+ import type { TmdbSearchResult } from '../types'
+ import { posterUrl } from '../utils/poster'
```
`:57`：`` :src="`${TMDB_POSTER_BASE}${selected.poster_path}`" `` → `:src="posterUrl(selected.poster_path)"`。

**ApprovalsView.vue**
```diff
- import { TMDB_POSTER_BASE, type ApprovalItem, type TmdbSearchResult } from '../types'
+ import type { ApprovalItem, type TmdbSearchResult } from '../types'
+ import { posterUrl } from '../utils/poster'
...
- function poster(url: string | null): string | null {
-   return url ? `${TMDB_POSTER_BASE}${url}` : null
- }
```
`:169` 模板 `poster(item.poster_path)` → `posterUrl(item.poster_path)`，删除 `poster` 函数定义。

**types/index.ts**
```diff
- export const TMDB_POSTER_BASE = 'https://image.tmdb.org/t/p/w500'
```
（同时更新 :45 注释，移除「配合 TMDB_POSTER_BASE 拼完整 URL」措辞 → 「前端经 posterUrl 走后端代理」）

- [ ] **Step 2: 全仓 grep 确认 TMDB_POSTER_BASE 无残留**

Run: `grep -rn "TMDB_POSTER_BASE" frontend/src/`
Expected: 无输出（0 匹配）

- [ ] **Step 3: 运行前端测试 + 构建**

Run: `npm test && npm run build`（workdir: `frontend`）
Expected: vitest PASS + `vue-tsc --noEmit` 无类型错误 + `vite build` 成功（产物写入 `backend/static`）

- [ ] **Step 4: 验证兜底保留（task 3.3）**

核对 5 个改动文件，确认以下兜底逻辑原样保留（代码事实核对，非手工运行）：
- MediaListView：`imgErrors.has(m.id)` / `@error="imgErrors.add(m.id)"` / `lc-poster-fallback` 分支
- MediaDetailView：`posterBroken` / `@error="posterBroken = true"` / fallback 分支
- EmbyLibraryView：`posterErrors` / `onPosterError` 未被触碰（本任务未改该文件）
- TmdbSearch / MediaAddView / ApprovalsView 的 `v-else` 占位分支

预期：全部保留，未删除或弱化任何 fallback 逻辑。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/TmdbSearch.vue frontend/src/views/MediaListView.vue frontend/src/views/MediaDetailView.vue frontend/src/views/MediaAddView.vue frontend/src/views/ApprovalsView.vue
git commit -m "feat(poster): 前端海报统一走后端代理并删除直连常量"
```

---

### Task 6: 内存 TTL 缓存（tasks.md 4.1）

**Files:**
- Modify: `backend/app/services/poster.py`（缓存读写）
- Modify: `backend/tests/test_poster_proxy.py`（命中/过期/上限用例）

**Interfaces:**
- Consumes: `_POSTER_CACHE`（模块级 dict）、`time.monotonic()`
- Produces: `fetch_poster` 增加缓存层（对外签名不变）

- [ ] **Step 1: 写失败测试（缓存命中/过期/上限）**

追加到 `tests/test_poster_proxy.py`：

```python
def _fetch_calls_with_factory(monkeypatch, resp, calls):
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))


def test_cache_hit_skips_upstream(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/t/p/w500/x.jpg": (time.monotonic() + 100, "image/jpeg", b"cached")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"cached"
    assert calls == []  # 未触发回源


def test_cache_expired_refetches(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {"/t/p/w500/x.jpg": (time.monotonic() - 1, "image/jpeg", b"stale")},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    content, ctype = asyncio.run(poster_mod.fetch_poster("/t/p/w500/x.jpg"))
    assert content == b"fresh"
    assert calls == ["https://image.tmdb.org/t/p/w500/x.jpg"]


def test_cache_cap_drops_writes(monkeypatch):
    monkeypatch.setattr(
        poster_mod, "_POSTER_CACHE",
        {f"/t/p/w{k}.jpg": (time.monotonic() + 100, "image/jpeg", b"x") for k in range(poster_mod._POSTER_CACHE_MAX)},
    )
    calls: list[str] = []
    resp = SimpleNamespace(status_code=200, content=b"fresh", headers={"content-type": "image/jpeg"})
    monkeypatch.setattr(poster_mod, "_client_factory", _make_client_factory(resp, calls))
    asyncio.run(poster_mod.fetch_poster("/t/p/w500/overflow.jpg"))
    assert poster_mod._POSTER_CACHE.get("/t/p/w500/overflow.jpg") is None  # 未写入
```

- [ ] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_poster_proxy.py -v`
Expected: FAIL（3 个缓存用例）

- [ ] **Step 3: 实现缓存读写**

`services/poster.py`：`fetch_poster` 开头加缓存读，成功回源后加缓存写：

```python
async def fetch_poster(p: str) -> tuple[bytes, str]:
    now = time.monotonic()
    hit = _POSTER_CACHE.get(p)
    if hit is not None and now < hit[0]:
        return hit[1], hit[2]

    base = _base_url()
    url = f"{base}{p}"
    try:
        async with _client_factory() as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        _alert(p)
        raise Exception(f"网络请求失败: {exc}") from exc
    if resp.status_code != 200:
        _alert(p)
        raise Exception(f"上游返回 HTTP {resp.status_code}")

    ctype = resp.headers.get("content-type", "image/jpeg")
    # 上限未满才写入；失败路径不写缓存（避免临时故障期缓存错误状态）
    if len(_POSTER_CACHE) < _POSTER_CACHE_MAX:
        _POSTER_CACHE[p] = (now + _POSTER_CACHE_TTL, ctype, resp.content)
    return resp.content, ctype
```

- [ ] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_poster_proxy.py tests/test_poster_path_validation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/poster.py backend/tests/test_poster_proxy.py
git commit -m "feat(poster): 新增海报代理内存 TTL 缓存"
```

---

### Task 7: 全量验证（tasks.md 4.2）

**Files:**
- 验证用：`backend/tests/`（全量）、`frontend`（build + test）
- 新增（若缺失）：`backend/tests/test_poster_auth.py`

**Interfaces:**
- Consumes: 全部既有产物
- Produces: 通过证据（测试输出、构建产物）

- [ ] **Step 1: 补鉴权测试（未登录 401 / 登录 200）**

`backend/tests/test_poster_auth.py`（参考 `test_api_smoke.py` 的 TestClient + lifespan 模式）——本文件**仅断言未登录 401 事实**；登录态 200 链路由 `test_poster_proxy.py` 路由直调覆盖（user 传 fake + mock 回源），避免 TestClient 事件循环与 `_client_factory` monkeypatch 的隔离复杂度：

```python
"""海报代理端点鉴权测试（未登录 401）。"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lumencloud_poster_auth_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP
os.environ["JWT_SECRET"] = "poster-auth-secret-12345"
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_POSTER_PROXY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_poster_requires_auth():
    with TestClient(app) as client:
        # 未登录 → 401（不返回图片）
        r = client.get("/api/poster", params={"p": "/t/p/w500/x.jpg"})
        assert r.status_code == 401
```

- [ ] **Step 2: 运行全量后端测试**

Run: `python -m pytest tests/ -q`（workdir: `backend`）
Expected: 全部 PASS。若存在与本次 change 无关的 pre-existing 失败，记录失败用例名与根因（归因），不视为本 change 回归；但新增的 poster 相关用例必须全绿。

- [ ] **Step 3: 运行前端测试 + 构建**

Run: `npm test && npm run build`（workdir: `frontend`）
Expected: PASS（vitest）+ 构建成功（`backend/static` 更新）

- [ ] **Step 4: 记录构建证据（guard 依赖）**

```bash
comet state record-check media-poster-proxy build --command "npm test && npm run build" --exit-code 0
```

> 说明：如 guard 已自动探测到 npm 构建命令并成功执行，可跳过；否则人工记录。

- [ ] **Step 5: Commit 剩余变更（如有）并确认 tasks.md 全勾选**

```bash
git add -A
git commit -m "test(poster): 新增海报代理鉴权测试"
```

按 tasks.md 勾选全部 9 个任务（1.1-4.2），随后将完成证据交回 Comet Build 流程（guard build --apply）。