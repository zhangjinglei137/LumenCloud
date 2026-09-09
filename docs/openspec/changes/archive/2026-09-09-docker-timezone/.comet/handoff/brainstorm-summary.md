# Brainstorm Summary

- Change: docker-timezone
- Date: 2026-09-09

## 确认的技术方案

用户已拍板：**后端补 Z 方案**（spec.md 原文「后端返回时间字段明确标注时区」保持成立，不回退为前端契约方案）。

技术设计（横切 Docker / 后端 / 前端 三层）：

1. **Docker 层**：docker-compose.yml 与 docker-compose.prod.yml 的 lumencloud + db 服务各加 `TZ: Asia/Shanghai` 环境变量；用 `docker compose config` 验证解析。
2. **后端「补 Z」落地（关键设计）**：新增加一个统一序列化点（自定义 JSONResponse / datetime 序列化 handler），对所有 naive UTC datetime 输出自动追加 `Z`（aware datetime 规范为 UTC+Z）；`queue.py:_iso()` 同步补 Z。DB 存储保持 naive UTC 不迁移。遗留显式 isoformat 点核查记录，不影响 API 响应面。
3. **前端（核心）**：`format.ts` 增加 UTC 解析层——无时区后缀 ISO 先按 `new Date(iso + 'Z')` 解析（兜底兼容存量 naive 字段），带后缀按绝对时刻解析；渲染统一 `Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai' })`。`timeAgo`/`timeUntil` 基于绝对时刻差天然正确。视图层 29 处调用点全走统一入口，无需逐视图改；`LogsView.vue:50` 时长相对差核查记录（不改造）。
4. **测试策略**：`format.test.ts` 补东八区固定输出断言（naive + Z 输入）、timeAgo 跨时区一致性（固定 now），vitest 通过。

## 关键取舍与风险

- 补 Z 必须走统一序列化点，禁止逐字段手改（避免漏网，违背 D2a 否决理由）。
- naive 输入前端仍按 UTC 解析兜底（后端补 Z 前/遗漏字段不双错）。
- 存量历史 naive 数据按 UTC 解析后展示一致，无需回填。
- APScheduler 调度语义不动（非目标）。
- 浏览器本地时区不再影响展示（本轮核心修复点）。

## 测试策略

- 单测：formatTime 固定时刻断言（naive 输入 → 东八区期望值；带 Z 输入一致）、timeAgo 跨时区一致性。
- 验证命令：前端 vitest 全绿；`docker compose config` 解析通过。

## Spec Patch

- spec.md「后端时间字段时区语义明确」措辞收敛为「统一 UTC+Z 标注输出」，与补 Z 方案一致（待最终确认后回写）。