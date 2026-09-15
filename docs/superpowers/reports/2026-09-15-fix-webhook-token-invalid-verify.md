# Verification Report: fix-webhook-token-invalid

> 日期：2026-09-15 ｜ workflow: hotfix ｜ verify_mode: full（任务数 6 > 3 触发） ｜ review_mode: off

## 结论

**ALL CHECKS PASSED — 可进入归档。**

生产 401（`webhook token 无效`）根因已消除：NaSTools「消息通知→Webhook」渠道实发 `Authorization: <裸token>`（无 Bearer 前缀，源码证据 `app/message/client/webhook.py:201-206`），本系统此前仅接受 `X-NaSTools-Token` / `Authorization: Bearer <token>`，三格式互不匹配导致恒 401。修复后端点接受三通道，401/503 语义与 query 通道移除决策均不变。

## Summary

| 维度 | 状态 |
|------|------|
| Completeness | 6/6 tasks 完成；0 delta spec（skip_specs: true，鉴权通道细节无 spec 契约） |
| Correctness | 需求实现映射 1/1（RED→GREEN 验证：裸 token 401 → 200） |
| Coherence | design.md 决策全部落地；无 DRIFT |

## 检查项明细（full 模式）

| # | 检查项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | tasks.md 全部任务已完成 | ✅ PASS | 6/6 `[x]`，0 未勾选（grep 确认） |
| 2 | 实现符合 design.md 高层设计 | ✅ PASS | `_token_from_request` 三通道并列（nastools_notify.py:96-118），与 design「提取顺序 X-NaSTools-Token → Authorization(Bearer→裸 token)」一致 |
| 3 | 实现符合 Design Doc | ✅ PASS | hotfix 预设无独立 Design Doc（design.md 即设计文件），无偏差 |
| 4 | 能力规格场景全部通过 | ✅ PASS | 无 delta spec（提交流程无 spec 级行为变更），skip_specs: true；既有 `test_nastools_notify.py` 31 用例含新增裸 token 通道与 fail-closed 负例全绿 |
| 5 | proposal.md 目标已满足 | ✅ PASS | 三通道兼容落地、文档同步、`?token=` 引导移除，401 根因消除 |
| 6 | delta spec 与 design doc 无矛盾 | ✅ PASS | 无 delta spec，N/A |
| 7 | 关联设计文档可定位 | ✅ PASS | `docs/影视下载两队列重设计.md` §12.2/12.3/12.4 已同步修正（设计文档本身即本次改动对象） |

## 代码审查

- `review_mode: off`（hotfix 默认）：跳过自动代码审查，原因已记录。
- 手工集成审查（本次 diff 4 文件，48+/19-）：
  - 正确性：裸 token 通道经 `hmac.compare_digest` 常数时间比较（`_seconds_compare`），比较逻辑未改动，仅扩展提取源
  - 安全：无硬编码密钥（token 源自 DB/env）；`Authorization: Basic` 等其它 scheme 按原值比较失败 → 401（fail-closed 正确）；`?token=` query 通道未恢复（2026-09-12 攻击面决策保持）
  - 边界：空 header/缺失 → `None` → 401；`Bearer ` 前缀与裸 token 冲突时 Bearer 优先解析（与原行为一致）

## 验证证据

- RED 复现：`test_token_via_header_ok[Authorization-test-nastools-token-*]` 修复前 401 FAIL → 修复后 200 PASS
- 回归：`test_nastools_notify.py`（31，含裸 token/Basic/空 token/空白 fail-closed 负例）+ `test_notify.py` + `test_notify_templates.py` 全部通过
- 全量：`cd backend && .venv/bin/python -m pytest tests/ -q` = **670 passed**（record-check exit 0）
- 审查后补测（council 推荐项）：小写 bearer → 200、裸 token 错误值 → 401、`Authorization: Basic` → 401、Bearer 空 token/纯空白 → 401，全绿
- `git diff --check` 干净；改动文件与 tasks 描述一致（4 文件：config.py / nastools_notify.py / test_nastools_notify.py / 影视下载两队列重设计.md）

## 审查后追加修订（council 推荐项，2026-09-15）

- `_token_from_request` docstring 措辞修正：「生产已实测复现」→「生产已观测持续 401，修复前经测试级 RED 复现」（消除证据强度误导，k3 建议）
- 修复 `_secret()` docstring typo：`systen_config` → `system_config`（三位审查员独立发现）
- 归档 proposal.md / design.md 文档范围修正：§12.2/§12.4 → §12.2/§12.3/§12.4（ds 建议）
- 不采纳 scheme 白名单加固（glm 主张，多数派裁定维持实现），改以 `Authorization: Basic` → 401 负例固化 fail-closed 语义

## 已知后续项（不阻塞归档，记录在案）

- 生产生效验证交由用户在真实 NaSTools 侧确认：修复后 401 应消除，无需改动 NaSTools 配置。若生产 NaSTools 版本无 Authorization 能力（旧版插件），需升级至「消息通知→Webhook」渠道。