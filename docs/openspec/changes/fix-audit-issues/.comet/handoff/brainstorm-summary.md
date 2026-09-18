# Brainstorm Summary

- Change: fix-audit-issues
- Date: 2026-09-17

## 确认的技术方案

基于 open 阶段 design.md 的 10 个决策（D1-D10）深化：

1. **D1 数据模型**：单一 alembic 迁移。CASCADE 表：EpisodeState、TaskQueue、DownloadQueue（media_id FK）、DownloadTask（若有 media FK）；SET NULL 表：WatchRequest.requested_by/reviewed_by、InviteCode.used_by 等引用类；TaskRun.media_id 对齐 BIG_PK；media_id 补独立索引
2. **D2 鉴权**：users.token_version + JWT payload `ver` 字段，get_current_user 校验，改密递增
3. **D3 限流**：抽象 auth.py 现有 `_login_rate_key` 为通用进程内限流器，注册接口复用
4. **D4 登出**：登出接口 delete_cookie + 前端先调登出再清 localStorage
5. **D5 CAS 修复**：统一「WHERE 状态门控 + rowcount 校验」模式，写路径 409 / 内部路径标记 conflict
6. **D6 口径**：pending_estimate 查 DownloadQueue；前端活跃状态补 pending
7. **D7 缓存**：OrderedDict LRU 有界化（tmdb/emby/poster），poster 加 content-type 校验
8. **D8 CI**：.github/workflows/ci.yml 三 job（pytest/vitest+build/docker）
9. **D9 scan SQL**：PG 分支 func.make_interval 或 literal_column 稳妥写法 + 双方言渲染单测
10. **D10 首启**：system_config 空表时 job 默认 paused

## 关键取舍与风险

- **B8 回调鉴权已存在**（HMAC+防重放+fail-closed，notify.py 已验证）→ 该需求降级为「现有实现已满足 + 补测试封闭」，不新增鉴权代码
- **A6 Emby status**：小写值透传可能失效 → 保守 `.capitalize()` 映射 + 单测（契约以 Emby 实际返回验证）
- token_version 改密即踢在线会话 → 前端提示重新登录（预期语义）
- 单 worker 容量告警冷却：注释+文档明示 trade-off，不引入分布式状态
- CASCADE 误删风险 → 仅对确认的队列/状态表声明，引用类 SET NULL，现有删除测试验证

## 测试策略

- 每个 high/critical 修复配套新增单测（注册限流、token_version、CAS 冲突、pending 口径、CASCADE 删除、scan SQL 渲染）
- CI 让 70+ 现有测试自动运行作为回归闸门
- 前端 vitest 补充控制面操作测试与 401/redirect 防御测试

## Spec Patch

无。12 个 delta spec 已在 open 阶段完成，深度设计未发现需回写的 spec 变更（B8 现状满足 spec 的「确认持有合法鉴权信号」条款）。