# Proposal: emby-library-all-poster

## Why

Emby 影视库选择「全部」类型时列表为空（诊断 `GET /api/emby/library?library_id=7` 返回 `{items: [], total: 0, item_type: null}`），用户无法浏览全部媒体；同时 Emby 封面由 `<img>` 直连带 `api_key` 的 Emby 服务地址，未走后端代理缓存，整页刷新时大量并发回源导致封面加载缓慢。

## What Changes

- **修复「全部」类型空列表**：诊断并修复 library_id 聚合查询在「全部」场景返回空的问题（核查 ParentId 解析、IncludeItemTypes 映射、聚合去重逻辑与错误归一），确保全部类型能返回非空条目
- **Emby 封面改走后端代理加载**：复用/扩展海报代理通道，Emby 封面 URL 不再由前端直连（去掉 URL 内嵌 api_key 暴露），改由后端代理拉取并附带缓存头（复用进程内 TTL + 浏览器 Cache-Control），缓解整页刷新并发回源
- 保持「全部」聚合去重语义（多库条目按 emby_id 去重）与分类 Tab 行为不变

## Capabilities

### New Capabilities
- `emby-library-browse`: Emby 影视库浏览与封面加载（全部类型聚合查询、封面代理加载）

### Modified Capabilities
- `poster-proxy`: 前端统一走代理加载海报（代理范围扩展覆盖 Emby 封面源）

## Impact

- 后端：`backend/app/services/emby.py`（全部查询修复、封面 URL 生成改造）、`backend/app/routers/emby.py`（接口层如需）、`backend/app/services/poster.py` + `backend/app/routers/poster.py`（Emby 封面代理支持）
- 前端：`frontend/src/views/EmbyLibraryView.vue`（封面 src 走代理、加载兜底）、`frontend/src/utils/poster.ts`
- 依赖：Emby 服务端（Items 查询、图片接口）；无数据库变更
