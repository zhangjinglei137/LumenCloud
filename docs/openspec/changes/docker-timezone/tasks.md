# Tasks: docker-timezone

## 1. Docker 容器时区

- [x] 1.1 docker-compose.yml 的 lumencloud 与 db 服务添加 `TZ: Asia/Shanghai` 环境变量，验证 compose config 解析通过
- [x] 1.2 docker-compose.prod.yml 同样添加，验证 compose config 解析通过

## 2. 前端东八区格式化

- [x] 2.1 format.ts 改造 formatTime/timeAgo：无时区后缀的 ISO 先按 UTC 解析（补 Z），再显式按 Asia/Shanghai 渲染（Intl timeZone），验证单测覆盖带/不带时区后缀输入
- [x] 2.2 全仓 grep formatTime/timeAgo 调用点确认全部走统一入口，验证各视图（队列/影视库/详情/日志/设置）时间展示不直接 new Date 本地渲染
- [x] 2.3 前端测试补充：东八区格式化（固定时间断言输出）、timeAgo 跨时区一致性用例，验证 `vitest` 通过

## 3. 后端时间语义核查

- [x] 3.1 核查后端 API 时间字段输出：确认统一为 UTC naive 或带 Z，前端按 UTC 解析成立（不逐字段改造，仅核查并记录约定），验证无字段带时区偏移造成双重换算

## 4. 验证

- [x] 4.1 手动验证：部署后容器 date 为 CST；前端各页时间 = 东八区且不同时区浏览器一致，验证 `npm run build` 通过
