## Why

用户反馈 Docker 部署下即使配置了 `TZ: Asia/Shanghai`，容器/应用展示时间仍非东八区。现状：docker-compose 未显式注入 TZ；后端统一以 UTC naive 时间写入数据库（`now_utc_naive()`、server_default CURRENT_TIMESTAMP）；前端 `new Date(iso)` 依赖浏览器本地时区解析。需要让展示时间与东八区一致。

## What Changes

- **Docker 层**：docker-compose.yml / docker-compose.prod.yml 的 lumencloud 服务注入 `TZ: Asia/Shanghai` 环境变量（同时为 db 服务注入，保证 postgres 时间语义一致）
- **后端**：明确时间语义——数据库继续存 UTC（避免破坏既有数据与 naive/aware 一致性），由展示层负责时区换算；排查并统一各 API 返回时间字段的时区标注（确保前端可正确换算）
- **前端**：时间格式化显式按东八区（Asia/Shanghai）渲染，不依赖浏览器本地时区；formatTime/timeAgo 统一走东八区
- 覆盖：任务/下载队列、影视库/详情、运行日志等所有时间展示点

## Capabilities

### New Capabilities
- `timezone-display`: 时间展示统一为东八区（Asia/Shanghai）的能力，覆盖后端时间语义与前端格式化

### Modified Capabilities
<!-- 时间展示为横切能力，无既有 spec 的单项需求级变更（各模块时间字段语义统一由本 capability 约束）。 -->

## Impact

- Docker：docker-compose.yml / docker-compose.prod.yml（TZ 环境变量）
- 后端：时间返回字段的时区标注核查（如带 +08:00 或明确 UTC）；不改变 DB 存储（保持 UTC）
- 前端：`frontend/src/utils/format.ts`（formatTime/timeAgo 东八区渲染）、各视图时间展示点
- 无数据库迁移（存储语义不变）
