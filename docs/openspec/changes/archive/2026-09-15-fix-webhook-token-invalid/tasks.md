# fix-webhook-token-invalid 修复任务清单

> 语言：zh-CN ｜ workflow: hotfix ｜ build_mode: direct ｜ tdd_mode: direct ｜ review_mode: off

## 任务

- [x] 1. RED：在 `backend/tests/test_nastools_notify.py` 的 `test_token_via_header_ok` 参数化列表追加裸 token 用例（`("Authorization", _TOKEN)`），运行确认该用例失败（401），证明复现
- [x] 2. 修复 `backend/app/routers/nastools_notify.py` 的 `_token_from_request`：支持 `Authorization: <裸token>`（无 Bearer 前缀）通道，保持 `X-NaSTools-Token` / `Bearer` 通道与 401/503 语义不变
- [x] 3. 同步更新模块 docstring 与 `backend/app/config.py:85-89` 注释：三通道并列、标注 NaSTools 实发裸 token（附源码证据）
- [x] 4. 修正 `docs/影视下载两队列重设计.md` §12.2/§12.3/§12.4：删除 `?token=` 引导，改为新版渠道 Authorization 裸 token 说明
- [x] 5. 全量回归：运行 `backend` 下 `test_nastools_notify.py`、`test_notify.py`、`test_notify_templates.py`，全部通过
- [x] 6. 根因消除检查：确认 `_token_from_request` 已无「NaSTools 实发格式恒被拒」路径，提交代码（message: `fix(nastools): 兼容 NaSTools Webhook 渠道裸 token Authorization`）