# fix-webhook-token-invalid 修复方案

## 背景与根因

生产环境 NaSTools（thntime.fun:3000）通过新版「消息通知→Webhook」渠道向本系统 `POST /internal/nastools/notify` 推送事件，持续收到 401 `{"detail":"webhook token 无效"}`。

**根因**（源码级调研确认，来源：`app/message/client/webhook.py` 多仓库交叉验证）：

- NaSTools 渠道发送时构造 `Authorization: <裸token>`（**无 Bearer 前缀**），见 `webhook.py:201-206`：
  ```python
  r = {"Content-Type": "application/json"}
  if self._token:
      r['Authorization'] = self._token   # ← 裸 token，无 Bearer
  ```
- NaSTools 从不发送 `X-NaSTools-Token`（全源码无此 header）。
- 本系统 `_token_from_request`（`nastools_notify.py:93-101`）只接受：
  1. `X-NaSTools-Token`（NaSTools 从不发）
  2. `Authorization: Bearer <token>`（NaSTools 发的是裸 token，无 Bearer）
- 三种格式互不匹配 → `provided` 恒为 `None` → 401。

## 修复方案

### 变更点：`backend/app/routers/nastools_notify.py`

`_token_from_request` 增加第三条通道：`Authorization: <裸token>`（无 Bearer 前缀时按原值提取）。

```python
def _token_from_request(request: Request) -> Optional[str]:
    """header `X-NaSTools-Token` → `Authorization`（Bearer 前缀或裸 token，后者兼容 NaSTools 新版消息通知 Webhook 渠道）。"""
    token = request.headers.get("X-NaSTools-Token")
    if token:
        return token
    auth = request.headers.get("Authorization")
    if auth:
        auth = auth.strip()
        if auth.lower().startswith("bearer "):
            return auth[len("bearer "):].strip()
        # 兼容 NaSTools 新版「消息通知→Webhook」渠道：Authorization: <裸token>（无 Bearer 前缀）
        return auth
    return None
```

要点：
- 提取顺序不变：`X-NaSTools-Token` → `Authorization`（Bearer → 裸 token 两种解析）。
- 鉴权语义不变：提取出的值仍与 `_secret()` 常数时间比较（`_seconds_compare`），不匹配 → 401；secret 未配置 → 503（fail-closed）。
- 安全边界：裸 token 通道与 Bearer 通道等价——都是「请求头携带 secret 与配置值常数时间比较」，不引入新攻击面；`?token=` query 通道维持移除状态（2026-09-12 决策），不恢复。
- 注意：`Authorization: Basic ...` 等其它 scheme 会被按裸值提取后比较失败 → 401，行为正确。

### 回归测试：`backend/tests/test_nastools_notify.py`

沿用既有 `test_token_via_header_ok` 参数化模式，新增裸 token 用例：

```python
("Authorization", _TOKEN),  # NaSTools 新版 Webhook 渠道：裸 token，无 Bearer 前缀
```

### 文档同步（消除过时指导）

1. `nastools_notify.py` 模块 docstring：更新鉴权说明——三通道并列，明确标注 NaSTools 实际发送裸 token（附源码证据位置），移除「旧版插件需升级」的未决假设表述（已核实为实发格式而非假设）。
2. `backend/app/config.py`:85-89 注释：同步说明渠道实发裸 token。
3. `docs/影视下载两队列重设计.md` §12.2（L507-511）与 §12.4（L534）：删除 `?token=` 配置引导（已失效），改为「新版消息通知→Webhook 渠道配置 Authorization 裸 token」。

## 验证方式

- 新增回归测试转绿（裸 token 200、Bearer 200、X-NaSTools-Token 200、缺失/错误 401、query 移除 401 全保持）。
- 全量运行 `backend` 相关测试套件（至少 `test_nastools_notify.py`、`test_notify.py`、`test_notify_templates.py`）。
- 根因消除检查：确认 `_token_from_request` 中不再存在「NaSTools 实发格式恒被拒」的代码路径。
- 生产验证（由用户执行）：NaSTools 端无需改动，webhook 401 应消除。