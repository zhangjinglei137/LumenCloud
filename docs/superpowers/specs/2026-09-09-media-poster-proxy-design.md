---
comet_change: media-poster-proxy
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-09-media-poster-proxy
status: final
---

# 影视海报图床代理（media-poster-proxy）设计文档

- Date: 2026-09-09
- Canonical spec: `docs/openspec/changes/media-poster-proxy/specs/poster-proxy/spec.md`
- OpenSpec handoff: `docs/openspec/changes/media-poster-proxy/.comet/handoff/design-context.md`

## 1. 背景与目标

影视库所有影视的 `poster_path` 均有值，但海报全部不显示——前端直连 `https://image.tmdb.org/t/p/w500` 图床在墙内网络不可达。本 change 为影视海报提供后端代理加载能力，前端所有海报展示统一走后端代理，并支持可配置图床镜像地址。

**Goals**
- 后端新增海报代理端点 `GET /api/poster`，代理拉取 TMDB 图床图片，校验路径合法性（防 SSRF/路径穿越），返回图片与缓存头
- 支持配置图床镜像根地址 `tmdb_poster_proxy`（config_store DB 优先、env 兜底，保存即生效）
- 前端统一经后端代理加载海报，保留既有加载失败兜底（标题占位）

**Non-Goals**
- 不改 TMDB 元数据获取逻辑（poster_path 落库逻辑不变）
- 不做图片持久化存储（仅浏览器 HTTP 缓存头 + 可选进程内 TTL 缓存）
- 不处理 Emby 库自身海报（Emby 条目 `poster_url` 为完整 URL，与 TMDB 相对路径语义不同，继续直连 Emby）

## 2. 架构总览

```
浏览器 <img src="/api/poster?p=/t/p/w500/xx.jpg">   （同源，自动携带 access_token cookie）
   │  Cookie 鉴权：get_current_user（未登录 → 401）
   ▼
POST /api/poster  （backend/app/routers/poster.py，挂入 api_router 的 /api 前缀）
   │
   ▼
services/poster.py
   ├─ 1. _validate_poster_path(p)  → 非法 400（不发起对外请求）
   ├─ 2. 内存 TTL 缓存查询（path → bytes）→ 命中直接返回
   ├─ 3. _poster_base() 镜像/回退选择 + 误填防御校验（PosterUnavailable → 503）
   └─ 4. httpx.AsyncClient GET {base}{p}  → 成功 bytes+Content-Type+Cache-Control
                                        → 非 2xx/网络异常 → 502 + 节流告警
```

## 3. 详细设计

### 3.1 端点契约

`GET /api/poster?p=<path>`

| 场景 | 状态码 | 响应 |
|------|--------|------|
| 合法路径回源成功 | 200 | 图片 bytes；`Content-Type` 透传上游（image/jpeg 等）；`Cache-Control: public, max-age=86400` |
| 非法路径（SSRF/穿越向量） | 400 | `{"detail": "..."}`，**不发起对外请求** |
| 未登录 | 401 | `{"detail": "未提供身份令牌"}`（与全站一致） |
| 镜像配置误填（防御校验） | 503 | `{"detail": "<明确错误提示>"}`（对齐 tmdb.py TMDBUnavailable→503 语义） |
| 回源失败（非 2xx / 网络异常 / 超时） | 502 | `{"detail": "海报代理拉取失败: ..."}` + 节流告警 |

参数约束：`p: str = Query(min_length=1, max_length=512)`。

### 3.2 路径校验（`_validate_poster_path`，SSRF/穿越防护）

```
校验规则（全部满足才通过）：
1. p 非空、非纯空白
2. p 以 "/" 开头（拒绝协议段 http://、//host 形态）
3. 不含 "://"、不包含反斜杠 "\"、不含 null 字节
4. posixpath.normpath(p) 归一化后仍以 "/t/p/" 开头，且归一化结果不等于 "/t/p"、"/t/p/.." 等越界形态
   ——normpath 会把 "/t/p/w500/../xx" 归一化，双重编码 "%2e%2e" 经 FastAPI query
   自动 URL 解码后进入同一校验路径，穿越形态被 normpath 消化后判定失败
```

返回 `bool`；非法 → 路由直接 400，不触达回源逻辑。

要点：
- FastAPI Query 参数天然已解码一次，`%2e%2e` → `..`、`%2f` → `/`，因此**在解码后**的统一校验入口做 normpath 复核即可覆盖双重编码绕过
- 不使用 `urllib.parse.urlsplit` 之外的解析（p 为纯路径，校验成功后才与 base 拼接）

### 3.3 图床镜像配置（`tmdb_poster_proxy`）

**配置读取**（每次请求函数内读取，保存即生效，与 `_base_url` 同模式）：

```python
POSTER_DEFAULT_BASE = "https://image.tmdb.org"
def _poster_base() -> str:
    mirror = (config_store.get("tmdb_poster_proxy", settings.TMDB_POSTER_PROXY) or "").strip().rstrip("/")
    if mirror:
        # 复用 tmdb.py _base_url 误填防御：无 scheme 的 host:port（如 192.168.3.31:7897）
        # 一定是误填的科学上网代理端口 → 明确报错（PosterUnavailable → 503）
        if "://" not in mirror and ":" in mirror:
            raise PosterUnavailable("...与 tmdb_proxy 同款提示，指引填写反代根地址...")
    return mirror or POSTER_DEFAULT_BASE
```

- 语义：镜像为图床反代**根地址**，仅替换域名（image.tmdb.org → 镜像），路径 `/t/p/...` 原样拼接
- 存储：`system_config`（settings 页 PATCH 白名单）+ env `TMDB_POSTER_PROXY` 兜底
- 新增注册点：
  1. `backend/app/config.py`：`TMDB_POSTER_PROXY: str = ""`（pydantic settings 字段）
  2. `backend/app/routers/settings.py` `_WHITELIST_EXACT`：加 `"tmdb_poster_proxy"` → 自动进 `editable_keys`，SettingsView 凭据表单「TMDB」组自动渲染为文本框
  3. `frontend/src/config/settingsMeta.ts`：补 `tmdb_poster_proxy` 条目（label「图床镜像地址」等），若 `getSettingMeta` 对未知键有缺省兜底且展示可接受，此项为可选项

### 3.4 鉴权（已确认：登录可调）

`user: User = Depends(get_current_user)`

- `get_current_user` 双通道：`Authorization: Bearer` header 优先，缺失时回退 httpOnly cookie `access_token`
- login 接口已 `Set-Cookie`（httpOnly，有效期与 JWT 一致），浏览器 `<img>` 同源请求**自动携带 cookie** → 已登录用户图片正常加载，无需前端任何额外处理
- 未登录 → 401 → `<img>` 触发 error 事件 → 前端既有 imgErrors/posterBroken/posterErrors 占位兜底，页面不破
- 收益：避免开放图床代理被外部滥用（消耗带宽/配额）；与全站鉴权一致

### 3.5 回源与错误处理

```python
async def fetch_poster(p: str) -> tuple[bytes, str] | None:
    # 1. 内存 TTL 缓存命中 → (bytes, content_type)
    # 2. base = _poster_base()  # 可能抛 PosterUnavailable → 503
    # 3. async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
    #        resp = await client.get(base + p)
    #    网络异常 httpx.HTTPError → 502 + 节流告警
    # 4. resp.status_code != 200 → 502 + 节流告警
    # 5. 成功 → 写 TTL 缓存 → 返回 (resp.content, resp.headers.get("content-type", "image/jpeg"))
```

- 直接 `resp.content` 内存缓冲：w500 海报通常几十 KB，无需 StreamingResponse，且 TTL 缓存需要完整 bytes
- 失败路径不写缓存（避免临时故障期间长时间占住错误状态）
- 节流告警：模块级 `_ALERT_COOLDOWN: dict[str, float]`，key = path，60s 内同 key 只 `logger.warning` 一次；每次请求尝试清理过期键，上限防护（超过阈值只清不增）

### 3.6 进程内 TTL 缓存（task 4.1）

```python
_POSTER_CACHE_TTL = 600       # 秒
_POSTER_CACHE_MAX = 100       # 条目上限
_POSTER_CACHE: dict[str, tuple[float, str, bytes]] = {}  # path → (expire_ts, content_type, bytes)
```

- 命中且未过期 → 直接返回（仍带浏览器 `Cache-Control: public, max-age=86400`）
- 过期 → 删除并回源；回源成功重新写入
- 超上限：回源成功但不写入（LRU 简化——直接拒绝写入，保上限稳定）
- 缓存为纯优化：任何异常降级直出，不阻断请求

### 3.7 前端统一走代理

**新增 `frontend/src/utils/poster.ts`**：

```ts
/** TMDB 海报相对路径 → 后端代理地址；空/null 返回 null（调用方已有占位兜底） */
export function posterUrl(path: string | null | undefined): string | null {
  if (!path) return null
  return `/api/poster?p=${encodeURIComponent(path)}`
}
```

**替换点清单**（`TMDB_POSTER_BASE` 常量删除，无残留引用）：

| 文件 | 位置 | 现有写法 | 替换为 |
|------|------|----------|--------|
| `views/MediaListView.vue` | 卡片 :146 / 表格 :207 | `` `${TMDB_POSTER_BASE}${m.poster_path}` `` | `posterUrl(m.poster_path)`（imgErrors 兜底保留） |
| `views/MediaDetailView.vue` | :164 | `` `${TMDB_POSTER_BASE}${detail.poster_path}` `` | `posterUrl(detail.poster_path)`（posterBroken 保留） |
| `components/TmdbSearch.vue` | `posterUrl()` :42-44 | `${TMDB_POSTER_BASE}${p}` | 委托 `utils/poster.ts` 的 `posterUrl`（覆盖 MediaAddView / ApprovalsView / EmbyLibraryView 对话框 3 个调用方） |
| `views/MediaAddView.vue` | :57 | `` `${TMDB_POSTER_BASE}${selected.poster_path}` `` | `posterUrl(selected.poster_path)` |
| `views/ApprovalsView.vue` | `poster()` :54-56 | `${TMDB_POSTER_BASE}${url}` | `posterUrl(url)` |

- `views/EmbyLibraryView.vue` 卡片 `m.poster_url`（Emby 完整 URL）**不动**（Non-Goal）；其 TmdbSearch 对话框经组件内部替换自动覆盖
- `poster_path` 契约不变（后端仍返回相对路径）
- `types/index.ts` 删除 `TMDB_POSTER_BASE` 常量（无引用后）
- 不需要改 `api/http.ts`（`<img>` 不走 axios；cookie 自动携带）

**前端单测（task 3.1，已确认引入 vitest）**：
- 新增 devDependency `vitest` + `frontend/vite.config.ts` 增加 test 配置
- `frontend/src/utils/poster.test.ts`：null/undefined/空串 → null；带 `/t/p/` 路径 → `/api/poster?p=%2Ft%2Fp%2F...`（encodeURIComponent 断言）；含特殊字符路径编码正确

### 3.8 settingsMeta 展示

`frontend/src/config/settingsMeta.ts` 新增 `tmdb_poster_proxy` 条目：
- 归属 CRED_GROUP「TMDB」（prefix=tmdb 自动归组，无需改 CRED_GROUP_*）
- label「图床镜像地址」、占位文案提示「反代根地址，如 https://tmdb-image.example.com；误填代理端口会报错」
- 若 `getSettingMeta` 对未知键已有可接受缺省（纯文本框展示），则此项仅作体验增强，非阻塞

## 4. 测试策略（TDD）

### 4.1 后端（pytest，`monkeypatch` 注入范式，参考 `backend/tests/test_tmdb_cache.py`）

**`tests/test_poster_path_validation.py`**（纯函数单测）：
- 合法：`/t/p/w500/ab12cd.jpg` → True
- 非法 → False：`../etc/passwd`、`/t/p/../../x`、`http://evil.com/x`、`//host/t/p/x`、空串、纯空白、`\`、null 字节 `\x00`、双重编码 `%2e%2e%2f`（解码后穿越）、`/t/p`（无子路径）、`/t/p/..`

**`tests/test_poster_proxy.py`**（服务层/路由，`monkeypatch.setattr("app.services.poster.httpx.AsyncClient", factory)`）：
- 回源成功：mock 200 + content + content-type → 断言返回 bytes 与 Content-Type、响应带 `Cache-Control: public, max-age=86400`
- 非 2xx（404/500/524）→ 502
- 网络异常（httpx.ConnectError）→ 502
- 镜像优先：注入 `config_store._cache = {"tmdb_poster_proxy": "https://mirror.example.com"}` → 断言请求 URL 前缀为镜像根
- 未配置回退官方：断言请求 URL 前缀 `https://image.tmdb.org`
- 误填防御：注入 `"192.168.3.31:7897"` → PosterUnavailable → 503 且**不发起请求**
- 缓存命中：首次回源后二次请求不再次触发 AsyncClient.get（mock call_count 断言）；缓存过期（缩短 TTL 或注入过期时间戳）→ 重新回源
- 缓存上限：注入 101 条 → 第 101 条回源成功但不写入

**`tests/test_poster_auth.py`**（或并入 test_poster_proxy）：未携带 token/cookie → 401；携带合法 cookie `access_token` → 200（构造方式参考现有 api 级测试对鉴权的处理）

### 4.2 前端（vitest）

- `posterUrl` 编码与 null 处理单测（见 3.7）
- `npm run build` 通过（TS 类型检查 + 打包）

### 4.3 验证顺序

1. 后端先写测试（红）→ 实现（绿）
2. 前端 posterUrl + 单测
3. 视图替换 → `npm run build`
4. 全量后端 pytest + 前端构建

## 5. 风险与取舍

| 风险 | 缓解 |
|------|------|
| 代理成为图片唯一路径，回源慢拖慢列表页 | 浏览器 `max-age=86400` + 内存 TTL 缓存（100 条）+ 上游通常可被 CDN 缓存 |
| SSRF / 路径穿越 | `/t/p/` 白名单 + normpath 归一化 + 拒绝协议段/反斜杠/null；双重编码经解码后复核；非法不发起请求 |
| 开放代理被滥用 | 登录鉴权（cookie 通道）；未登录 401 |
| 镜像地址误填（同 tmdb_proxy 代理端口问题） | 复用 `_base_url` 防御校验 + 明确错误文案（503） |
| 后端内存占用 | TTL 缓存条目上限 100，超限直出不缓存 |
| 缓存与上游不一致 | TTL 600s 短窗口；浏览器缓存由 max-age 控制，进程缓存仅回源层 |

## 6. 文件清单

**新增**
- `backend/app/services/poster.py`（校验 + 回源 + 缓存 + 节流）
- `backend/app/routers/poster.py`（端点 + 鉴权 + 错误映射）
- `backend/tests/test_poster_path_validation.py`
- `backend/tests/test_poster_proxy.py`
- `backend/tests/test_poster_auth.py`
- `frontend/src/utils/poster.ts`
- `frontend/src/utils/poster.test.ts`

**修改**
- `backend/app/config.py`（+`TMDB_POSTER_PROXY`）
- `backend/app/routers/api.py`（挂载 poster router）
- `backend/app/routers/settings.py`（白名单 +`tmdb_poster_proxy`）
- `frontend/src/types/index.ts`（删除 `TMDB_POSTER_BASE`）
- `frontend/src/views/MediaListView.vue`、`views/MediaDetailView.vue`、`views/MediaAddView.vue`、`views/ApprovalsView.vue`
- `frontend/src/components/TmdbSearch.vue`
- `frontend/src/config/settingsMeta.ts`（+`tmdb_poster_proxy` 展示元数据）
- `frontend/package.json` / `frontend/vite.config.ts`（+vitest）

**不修改**：`views/EmbyLibraryView.vue`（仅经 TmdbSearch 组件间接覆盖对话框海报）；Emby `poster_url` 逻辑

## 7. 关联产物

- Spec Patch：`specs/poster-proxy/spec.md` 已补「代理端点要求登录态」验收场景（design 阶段回写）
- Migration/回滚：无迁移依赖；旧 `TMDB_POSTER_BASE` 删除前已确认无残留引用；回滚 = 前端改回直连常量 + 后端路由保留无害
