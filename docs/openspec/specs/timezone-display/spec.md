# timezone-display Specification

## Purpose
保证 LumenCloud 所有时间展示统一为东八区（Asia/Shanghai）：Docker 容器时区正确、后端时间字段语义明确、前端格式化显式按东八区渲染，不依赖运行环境或浏览器本地时区。

## Requirements

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
