# Brainstorm Summary

- Change: media-poster-proxy
- Date: 2026-09-09

## 确认的技术方案

> 用户已于 2026-09-09 确认本方案（含鉴权与前端单测决策）。

### 后端：/api/poster 端点（新增 `backend/app/routers/poster.py` + `backend/app/services/poster.py`）
- `GET /api/poster?p=<TMDB图床相对路径>`；p 校验（`_validate_poster_path`）：
  - 必须以 `/t/p/` 开头；拒绝 `..`、`//`、协议段（`://`）、反斜杠、null 字节
  - 归一化防穿越用 `posixpath.normpath` 复核（防双重编码 `%2e%2e`，FastAPI query 自动解码后仍校验）
  - 非法 → 400，不发起对外请求（SSRF 防护）
- **鉴权：登录可调** —— `get_current_user`（header 优先、cookie `access_token` 兜底；login 已 Set-Cookie httpOnly，同源 `<img>` 自动携带）
- 回源：httpx.AsyncClient（timeout 10s）拉取；成功返回 bytes + 上游 Content-Type + `Cache-Control: public, max-age=86400`（浏览器缓存）
- 失败：非 2xx/网络异常 → 502 + 节流告警（模块级 `{key: ts}`，60s 内同键只 log 一次）；配置误填防御 → 503 明确报错

### 图床镜像配置：`tmdb_poster_proxy`
- `config.py` 新增 `TMDB_POSTER_PROXY: str = ""`；`routers/settings.py` `_WHITELIST_EXACT` 加 `tmdb_poster_proxy`（自动进 editable_keys + SettingsView 凭据表单）；config_store DB 优先、env 兜底，函数内读取保存即生效
- 语义 = 镜像反代根地址，替换 image.tmdb.org 域名，路径不变；未配置回退 `https://image.tmdb.org`
- 误填防御复用 tmdb.py `_base_url` 同款校验（无 scheme 的 `host:port` → PosterUnavailable → 503）

### 进程内 TTL 缓存（task 4.1）
- `{path: (expire_ts, content_type, bytes)}`，TTL 600s、条目上限 100，超限不缓存直接回源直出；命中仍带浏览器缓存头

### 前端统一走代理：`frontend/src/utils/poster.ts`
- `posterUrl(path)` → 空/null → null；否则 `/api/poster?p=${encodeURIComponent(path)}`
- 替换点：MediaListView 卡片+表格、MediaDetailView、TmdbSearch 内部（覆盖 MediaAddView/ApprovalsView/EmbyLibraryView 对话框）、MediaAddView 确认区、ApprovalsView
- Emby 库自身 poster_url 不动（Non-Goal）；imgErrors/posterBroken/posterErrors 占位兜底保留；`TMDB_POSTER_BASE` 常量删除
- `settingsMeta.ts` 补 `tmdb_poster_proxy` 展示元数据（若 getSettingMeta 无缺省）
- **前端单测：引入 vitest**，新增 vitest 配置 + `posterUrl` 单测（编码与 null 处理）

## 关键取舍与风险

- [代理成为图片访问唯一路径，回源慢列表页变慢] → 浏览器 max-age 86400 + 内存 TTL 缓存（100 条）
- [SSRF/路径穿越] → `/t/p/` 白名单 + normpath 归一化 + 拒绝协议段；双重编码经 FastAPI 解码后复核
- [开放代理被滥用] → 登录鉴权（cookie 通道）；未登录 401 → 前端占位兜底不破页
- [镜像误填（同 tmdb_proxy 代理端口问题）] → 复用防御校验 + 明确错误文案
- [内存占用] → 缓存条目上限 + TTL，超限直出不缓存

## 测试策略

- 后端 pytest（monkeypatch `httpx.AsyncClient`，参考 test_tmdb_cache.py 范式）：
  - `test_poster_path_validation.py`：合法 /t/p/... 通过；`../`、`http://`、`//host`、空、`\`、null、双重编码拒绝
  - `test_poster_proxy.py`：回源成功（Content-Type + Cache-Control 断言）、非 2xx → 502、网络异常 → 502、镜像优先/未配置回退官方、缓存命中/过期、未登录 401、误填配置 → 503
- 前端：vitest 单测（posterUrl 编码与 null）+ npm run build

## Spec Patch

- 已确认回写：poster-proxy spec.md 补「代理端点要求登录态」验收场景（未登录 401 不返回图片）