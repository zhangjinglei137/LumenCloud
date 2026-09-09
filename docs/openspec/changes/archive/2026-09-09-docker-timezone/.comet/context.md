# Comet Design Handoff

- Change: docker-timezone
- Phase: design
- Mode: compact
- Context hash: 8388cec0c72066dec268819d487e95dad97ed7d445a83ea8e23b8e8108303139

Generated-by: comet-handoff.sh

OpenSpec remains the canonical capability spec. This handoff is a deterministic, source-traceable context pack, not an agent-authored summary.

## docs/openspec/changes/docker-timezone/proposal.md

- Source: docs/openspec/changes/docker-timezone/proposal.md
- Lines: 1-25
- SHA256: 1d06fed349e95bc614e8d8e61d69b03f98f8e5d870484ae82641ec2303bed4e3

```md
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

```

## docs/openspec/changes/docker-timezone/design.md

- Source: docs/openspec/changes/docker-timezone/design.md
- Lines: 1-80
- SHA256: ef15dce94700b0e938016675c53c835e0baa205c4024e866b2569844825fd1d5

```md
# Design: docker-timezone

## Context

现状（参见 proposal.md - Why）：
- docker-compose.yml / docker-compose.prod.yml 均未给任何服务注入 TZ 环境变量（用户自行添加未生效，或加到了未持久化的 compose override）
- 后端统一时间源 `now_utc_naive()` = `datetime.now(timezone.utc).replace(tzinfo=None)`（UTC naive），DB server_default=func.now()/CURRENT_TIMESTAMP（PG 为 UTC，SQLite 为字符串）；`main.py:132` 也用 `datetime.now(timezone.utc)`
- 前端 `formatTime` = `new Date(iso)` + getFullYear/getHours（浏览器本地时区）；`timeAgo` = `Date.now() - new Date(iso)`（同样本地语义）
- 后端返回的时间字段大多为 naive ISO（无时区后缀），浏览器 `new Date("2026-09-09 01:15")` 会按本地时区解析（无后缀按本地时间），导致展示与东八区意图不符

## Goals / Non-Goals

**Goals**
- 容器（应用 + db）时区 = Asia/Shanghai
- 后端存储保持 UTC 不变（避免迁移风险），返回时间字段带明确时区标注
- 前端格式化显式按东八区渲染，跨浏览器一致

**Non-Goals**
- 数据库时间迁移/重算（存储保持 UTC）
- 改变调度器/任务的调度语义（APScheduler 与数据库比较逻辑不动，避免破坏现有时间比较）
- 历史数据展示修正（展示层统一换算即可覆盖存量）

## Decisions

### D1：容器注入 TZ 环境变量

**决策**：docker-compose.yml 与 docker-compose.prod.yml 的 lumencloud 与 db 服务各加 `TZ: Asia/Shanghai` 环境变量（db 的 postgres 镜像 entrypoint 会读取 TZ 设置容器时区）。

**理由**：用户已尝试但未生效的根因是 compose 未声明 TZ；显式写入 compose 文件即开箱即用，无需 override。db 一并设置保证 pg 的 CURRENT_TIMESTAMP 语义（PG 返回 UTC 实际与容器 TZ 无关，但容器内日志/now() 对齐）。

**备选**：仅文档指导用户加 TZ → 不解决默认体验，否决。

### D2：后端时间字段带时区标注，存储保持 UTC

**决策**：
- DB 存储：维持 `now_utc_naive()`（UTC naive），**不迁移**
- API 返回：在路由序列化处统一把 naive UTC 时间字段标注为 UTC（追加 `Z` 或转 aware UTC 再 isoformat），让前端能确定换算基准；或维持现状（naive 无后缀）由前端约定「按东八区解释」——需二选一

**子决策（D2a）**：采用「后端返回带 `+08:00` 的东八区 ISO」**不可行**——因为后端存储是 UTC，逐字段换算东八区需统一时间工具且易漏。改为**更稳妥方案**：后端返回时间字段统一追加 `Z`（UTC 标注，明确无歧义），前端拿到 UTC 后显式转东八区显示。

**理由**：明确时区标注（Z/UTC）是「前端可正确换算」的充分条件；东八区转换收敛在前端格式化层（D3），后端只需保证字段带时区语义，改动面最小、无漏网。

**备选**：
- 后端逐字段输出 +08:00 → 需引入 aware 时区工具改造所有序列化点，风险高，否决
- 前端按「naive 即东八区」解释（不改后端）→ 对 UTC 存储的 naive 值直接 +8 显示会差 8 小时，错误，否决

### D3：前端显式东八区格式化

**决策**：`formatTime` / `timeAgo` 增加东八区换算：
- 若输入带时区标注（Z 或 +08:00）→ `new Date(iso)` 得到绝对时刻，再用 `Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', ... })`（或手动 +8h）格式化
- 统一封装 `formatTimeCn(iso)` / 改造现有 formatTime/timeAgo 内部走东八区
- 所有视图时间展示改用统一入口，不再直接 `new Date(...)` 本地渲染

**理由**：显式 timeZone 选项跨浏览器一致；`Intl` 处理 DST/历史偏移正确；封装后各页面（队列/影视/日志）一处修改全量生效。

**备选**：浏览器本地时区 + 依赖用户浏览器为东八区 → 不满足「跨浏览器一致」，否决。

### D4：APScheduler 调度语义不动

**决策**：scheduler 的间隔触发基于 monotonic/UTC 语义，不改动；仅容器时区与展示层变化不影响任务触发逻辑（间隔型任务与时区无关）。

**理由**：避免引入调度时序回归。

## Risks / Trade-offs

- [后端给 naive UTC 字段加 Z 需遍历序列化点] → 若改动面大，改为前端约定「后端时间字段一律为 UTC naive，前端统一按 UTC 解析再转东八区」——即前端把无后缀 ISO 先按 UTC 解释（`new Date(iso + 'Z')`），避免逐字段改后端。**采用此方案**（D2 修订：后端不改，前端约定 UTC 解析），改动最小且语义明确
- [历史 naive 数据被浏览器按本地解析过，存量展示偏差] → 统一入口后全量按 UTC+8 渲染，存量数据展示一致；不单独修正历史值
- [timeAgo 相对时间跨时区] → 基于绝对时刻差计算，与展示时区无关，天然正确

## Migration Plan

1. docker-compose 两文件加 TZ 环境变量（lumencloud + db）
2. 前端 format.ts：formatTime/timeAgo 改为「按 UTC 解析（无后缀补 Z）+ 东八区渲染」（Intl timeZone=Asia/Shanghai）
3. 前端各视图时间展示点改用统一入口（grep 所有 formatTime/timeAgo 调用确认覆盖）
4. 验证：部署后 date 显示 CST；前端各页时间 = 东八区；不同时区浏览器一致
5. 回滚：还原 format.ts 与 compose TZ；无数据库影响

## Open Questions

无（D2 修订已定：后端不改，前端约定 UTC 解析 + 东八区渲染）。

```

## docs/openspec/changes/docker-timezone/tasks.md

- Source: docs/openspec/changes/docker-timezone/tasks.md
- Lines: 1-20
- SHA256: 9e9096fd13db2ca5330ec445a0ea0c37110d39904d31be28a1df85431f85e5ba

```md
# Tasks: docker-timezone

## 1. Docker 容器时区

- [ ] 1.1 docker-compose.yml 的 lumencloud 与 db 服务添加 `TZ: Asia/Shanghai` 环境变量，验证 compose config 解析通过
- [ ] 1.2 docker-compose.prod.yml 同样添加，验证 compose config 解析通过

## 2. 前端东八区格式化

- [ ] 2.1 format.ts 改造 formatTime/timeAgo：无时区后缀的 ISO 先按 UTC 解析（补 Z），再显式按 Asia/Shanghai 渲染（Intl timeZone），验证单测覆盖带/不带时区后缀输入
- [ ] 2.2 全仓 grep formatTime/timeAgo 调用点确认全部走统一入口，验证各视图（队列/影视库/详情/日志/设置）时间展示不直接 new Date 本地渲染
- [ ] 2.3 前端测试补充：东八区格式化（固定时间断言输出）、timeAgo 跨时区一致性用例，验证 `vitest` 通过

## 3. 后端时间语义核查

- [ ] 3.1 核查后端 API 时间字段输出：确认统一为 UTC naive 或带 Z，前端按 UTC 解析成立（不逐字段改造，仅核查并记录约定），验证无字段带时区偏移造成双重换算

## 4. 验证

- [ ] 4.1 手动验证：部署后容器 date 为 CST；前端各页时间 = 东八区且不同时区浏览器一致，验证 `npm run build` 通过

```

## docs/openspec/changes/docker-timezone/specs/timezone-display/spec.md

- Source: docs/openspec/changes/docker-timezone/specs/timezone-display/spec.md
- Lines: 1-41
- SHA256: 726f96ca1adaf51c30bd8fe7b2fbe9b95b2cb22ab4ee048fe125c2e9af641571

```md
## Purpose

保证 LumenCloud 所有时间展示统一为东八区（Asia/Shanghai）：Docker 容器时区正确、后端时间字段语义明确、前端格式化显式按东八区渲染，不依赖运行环境或浏览器本地时区。

## ADDED Requirements

### Requirement: Docker 容器时区为东八区

部署容器（lumencloud 应用与 db 服务）SHALL 配置 `TZ=Asia/Shanghai` 环境变量，使容器内系统时间与时间戳为东八区。

#### Scenario: 应用容器时区
- **WHEN** 使用 docker-compose 部署后进入 lumencloud 容器执行 date
- **THEN** 容器系统时间显示东八区（CST, UTC+8）

#### Scenario: 数据库容器时区
- **WHEN** 使用 docker-compose 部署后查看 db 容器
- **THEN** db 容器同样配置东八区时区，时间语义与应用一致

### Requirement: 后端时间字段时区语义明确

后端 API 返回的时间字段 SHALL 具有明确的时区语义：数据库存储保持 UTC（不迁移、不破坏既有数据），返回给前端的时间字段 SHALL 统一输出 UTC+Z 标注（naive UTC datetime 序列化时追加 `Z` 后缀，aware UTC 归一为 `Z`），使前端可正确换算为东八区，不得输出无时区标注导致浏览器按本地时区误解析。

#### Scenario: 返回带 UTC+Z 标注
- **WHEN** 后端返回某时间字段（如队列 updated_at）
- **THEN** 该字段输出为 UTC+Z 标注的 ISO 字符串，前端能据此正确显示东八区时间

#### Scenario: 存储保持 UTC
- **WHEN** 系统写入数据库时间
- **THEN** 仍按 UTC 存储，不发生时区迁移，不影响既有数据

### Requirement: 前端时间展示为东八区

前端所有时间格式化（formatTime / timeAgo 等）SHALL 显式按 Asia/Shanghai 时区渲染，不得依赖浏览器本地时区；同一时间在不同浏览器/设备上展示一致。

#### Scenario: 显式东八区格式化
- **WHEN** 前端渲染任意时间字段（队列、影视库、详情、日志）
- **THEN** 展示为东八区时间，且不随浏览器本地时区变化

#### Scenario: 时区一致性
- **WHEN** 两台不同时区的浏览器查看同一时间字段
- **THEN** 展示的时间一致（均为东八区）

```
