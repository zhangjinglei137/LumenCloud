# Brainstorm Summary

- Change: fix-online-issues
- Date: 2026-09-11

## 确认的技术方案

7 项线上问题修复 + 全代码安全审查，单 change：

1. **缺失集判定修正（D1）**：后端 media.py `_stats()` 的 missing 改为「TMDB 已开播集数 − available」，已开播集数取 episode_info_cache（`get_episode_info` 返回 air_date）过滤 air_date ≤ today 计数；缓存未命中回源 TMDB；外部服务故障降级为现状口径（total − available）不报错。total 字段仍返回 TMDB 全集数。
2. **状态收敛两值（D2）**：后端 create/patch 已强制 tracking/paused（保持）；前端 `mediaStatusLabel/Type` 未知值回退中文（download/downloading → 下载中），下拉仅两选项。
3. **Emby 封面路径修复（D3）**：`_normalize_library_item` poster_url 补前导 `/`（`/api/poster?p=/emby/{item_id}/Primary`），与 `_validate_poster_path` 前缀一致；补测试。
4. **用户角色只读（D4）**：UsersView 角色列 el-select → 只读 tag；后端 PATCH 接口保留（admin-only）前端不再暴露。
5. **订阅按钮按角色（D5）**：EmbyLibraryView 订阅按钮/批量入口/TMDB 对话框订阅入口加 `v-if="auth.isAdmin"`。
6. **队列访客权限（D6）**：`GET /queue/download/state` 降为 get_current_user；QueueView 控制按钮维持 auth.isAdmin 禁用/隐藏，guest 打开不再 403。
7. **安全审查（D7）**：后端认证/授权/注入/SSRF/敏感信息 + 前端 XSS/权限绕过系统审查，修复确认漏洞。

## 关键取舍与风险

- D1 依赖 episode_info_cache 完整性 → 回源兜底，故障降级现状口径
- D4 保留后端角色接口（UI 移除）→ 与需求一致；如需彻底关闭后端另行处理
- D6 状态接口放宽到登录用户 → 数据非敏感
- D7 审查范围广 → 高危/中危优先修复，低危记录

## 测试策略

- 后端 pytest：缺失集统计（未开播不计入）、poster 路径校验、队列状态接口 admin/guest 均可读、admin 用户用例回归
- 前端 vitest：episodeSummaryText 口径、状态字典兜底、订阅按钮 admin/guest 可见性
- 集成：`pytest` 全量、`vitest` 全量、`npm run build`

## Spec Patch

无（open 阶段 delta specs 已覆盖全部变更；design 阶段未发现新需求/验收场景缺口）
