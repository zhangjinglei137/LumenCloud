# Brainstorm Summary

- Change: emby-library-all-poster
- Date: 2026-09-11

## 现状核查结论（代码已核实）

### 「全部」空列表根因（确定性）

- 前端 `stores/emby.ts` 的 `libraryGroups` getter 分组逻辑：
  - `movies` → 电影组；`tvshows`（非动漫）→ 剧集组；`tvshows`（动漫）→ 动漫组；**仅 `mixed` / `null` 落入「全部」组**。
- `EmbyLibraryView.vue` `fetchCurrent()` 聚合态遍历 `libraryGroups['all']`：当环境中只有 movies/tvshows 库时 `libs.length === 0` → 直接置空态 → **「全部」Tab 必空**。
- 诊断 `GET /api/emby/library?library_id=7` 返回空：真实库的具体原因需 build 阶段连接真实 Emby 确认（ParentId 映射 / mixed 库 IncludeItemTypes / 库本身空 / 权限）。

### Emby 封面直连现状

- 后端 `_normalize_library_item`（emby.py:378）生成 `poster_url = {base}/Items/{item_id}/Images/Primary?api_key={api_key}` —— **api_key 内嵌、前端直连 Emby**。
- 现有 `poster.py` 代理：`/api/poster?p=<path>` 仅放行 `/t/p/` 前缀（TMDB 图床）；登录态 + 进程内 TTL(600s) + Cache-Control(86400) + SSRF/路径校验。
- 前端 `posterUrl()`（poster.ts）只组装 TMDB 路径；Emby 视图直接使用后端返回的完整 poster_url。

## 已确认决策

1. **「全部」聚合修复 = 方案 B（用户确认 2026-09-11）**：后端新增全部聚合端点，一次请求遍历全部影视类库并发拉取、去重、归一化；前端全部 Tab 改为单次请求。分类 Tab（movie/series/anime）与单库下钻保持现有前端逐库聚合行为不变。
2. **封面代理 URL 形态（候选）**：后端 `poster_url` 直接返回代理地址 `/api/poster?p=emby/<itemId>/Primary`，前端不组装、不直连 Emby。

## 后端设计要点（方案 B 细化）

- 服务层新增 `list_all_library(item_type, status)`：
  - 复用 `list_library_folders()` 取全部影视类库（movies/tvshows/mixed/null）
  - 逐库并发拉取（新增信号量 `_LIBRARY_FETCH_CONCURRENCY` 限并发，防打爆 Emby）
  - 归一化 + 按 emby_id 去重；逐库独立 catch：全部失败 → EmbyUnavailable；部分失败 → 保留成功部分 + warn 日志
  - 复用 `_attach_tmdb_series_status` + `_attach_in_media_flag`
- 新端点 `GET /api/emby/library/all?item_type=&status=`（独立路径，保持单库端点契约不变），响应与单库一致 `{items, total, item_type}`；错误码沿用 `emby_not_configured` / `emby_unreachable`
- 封面代理扩展（poster.py + routers/poster.py）：
  - `_validate_poster_path` 放行 `/emby/<itemId>/Primary`（itemId 白名单字符集，回源 URL = 配置 base + 固定路径，不接收任意 URL → 防 SSRF）
  - `fetch_poster` 按前缀分支：emby → 回源 `{emby_base}/Items/{id}/Images/Primary?api_key=`（配置来自 config_store，poster.py 独立读取避免循环依赖）；TMDB → 原逻辑
  - 缓存 key 前缀天然隔离；TTL 沿用 600s / Cache-Control 86400
  - `_normalize_library_item` 的 poster_url 改为代理地址（不再内嵌 api_key）

## 前端设计要点

- 新增 `listAllEmbyLibraryApi()`（`GET /api/emby/library/all`），全部 Tab 聚合态单次请求
- 分类 Tab / 单库下钻 / 动漫判定逻辑不变
- EmbyLibraryView 封面 img src 直接使用后端代理 poster_url，保留 posterErrors 占位兜底

## 关键取舍与风险

- [代理成为新的可滥用出口] → 仅接受受控 emby 图片标识 + 配置回源，不接收任意 URL
- [全部聚合多库请求放大] → 后端信号量限并发，逐库错误隔离
- [后端聚合与前端分类聚合语义重复] → 接受：全部 Tab 走后端聚合、分类 Tab 走前端逐库，各自保持语义

## 测试策略

- 后端 pytest：list_all_library（并发聚合/去重/部分失败/错误归一）、代理 emby 前缀合法/非法、未登录 401、api_key 不外泄、poster_url 为代理格式
- 前端 vitest：listAllEmbyLibraryApi 调用、全部 Tab 单次请求、封面兜底
- 真实 Emby 环境集成验证（任务 4.1）

## Spec Patch

- 无：现有 delta spec（emby-library-browse / poster-proxy）的验收场景已完整覆盖本设计（全部聚合非空、mixed 收录、不可达错误提示、封面代理显示/降级/api_key 不外泄），无需回写

## 状态

- 已确认（2026-09-11）：用户确认方案 B + 封面代理 URL 形态，进入 Design Doc 创建
