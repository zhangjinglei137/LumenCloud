# fix-webhook-token-invalid

## Why

生产环境 NaSTools 向本系统推送 webhook（`POST /internal/nastools/notify`）持续 401，响应 `{"detail":"webhook token 无效"}`，导致「整理完成→入库」回调加速链路失效。经调研确认根因：NaSTools 新版「消息通知→Webhook」渠道实际发送的是 `Authorization: <裸token>`（无 Bearer 前缀），而本系统鉴权仅接受 `X-NaSTools-Token` 或 `Authorization: Bearer <token>` 两种格式，双方契约不匹配，token 校验必然失败。

## What Changes

- `backend/app/routers/nastools_notify.py`：`_token_from_request` 增加对 `Authorization: <裸token>`（无 Bearer 前缀）格式的兼容，与既有 `X-NaSTools-Token`、`Authorization: Bearer <token>` 通道并列；401/503 语义均不变
- 同步修正过时文档：模块 docstring（已移 query 通道的事实沿革）、`config.py` 注释、设计文档 §12.2/§12.3/§12.4 的配置指导（删除 `?token=` 引导，改为新版渠道 Authorization 裸 token 说明）
- `backend/tests/test_nastools_notify.py`：新增裸 token Authorization 通道回归用例（RED 先行）
- 不改变任何 spec 定义的行为契约（鉴权通道细节在既有 spec 中无 requirement 描述，验收场景不变）

## Capabilities

### New Capabilities

无。

### Modified Capabilities

无。既有 spec（`pipeline-transfer`、`notifications`）均未定义 nastools webhook 的鉴权通道细节，本修复不改变任何 Requirement 或验收场景，仅修复与第三方 NaSTools 的实际发送格式兼容性。故 `.openspec.yaml` 设置 `skip_specs: true`。

## Impact

- **代码**：`backend/app/routers/nastools_notify.py`（`_token_from_request`，约 +5 行）+ 对应测试
- **文档**：`docs/影视下载两队列重设计.md` §12.2/§12.3/§12.4、`backend/app/config.py` 注释、模块 docstring
- **安全**：修复后允许 Authorization header 裸 token 与 secret 常数时间比较，行为与 Bearer 通道等价，无新增信息泄露面；query 通道维持移除状态（不恢复）
- **运行时**：无需数据库/配置变更；已配置的 `internal_nastools_webhook_token` 继续生效