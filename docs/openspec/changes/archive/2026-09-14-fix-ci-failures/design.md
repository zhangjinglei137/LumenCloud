# fix-ci-failures / design

## 修复方案（单一方案）

### 改动 1：main.py — SPA fallback 路由无条件注册，目录存在性下沉到请求时判断

`backend/app/main.py:151-168` 当前：

```python
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        ...
```

问题：`serve_spa` 注册被模块加载期的 `STATIC_DIR.exists()` 门控，CI 干净 checkout 无 static 产物 → 路由整体不注册 → 404。

改动：

1. `/assets` 挂载保留存在性门控（目录不存在时 StaticFiles 初始化会抛错，且无可挂载内容，保持现状安全）。
2. `serve_spa` 路由无条件注册，路径穿越防护逻辑不变；static 根不存在时 `file_path.is_relative_to(static_root)` 仍能安全判断（此时无文件可直出），未知路径 fallback 尝试读 `index.html`，不存在时返回 404 JSON（与 FastAPI 默认错误结构一致，前端可正常处理）。

要点：
- 穿越防护语义不变：`full_path` 先 resolve，必须落在 static 根内且是文件才 `FileResponse`，越界一律不直出。
- `STATIC_DIR` 缺失时 `serve_spa` 对非 api 路径 fallback `index.html`，文件不存在则 404 JSON，避免 500。
- 不改变生产有 static 产物时的行为（正常直出 + fallback）。
- 删除 `STATIC_DIR.exists()` 对 `serve_spa` 的包裹，但仍保留对 `/assets` StaticFiles 的挂载条件（避免初始化异常）。

### 改动 2：Dockerfile — 构建期 apt-get upgrade 系统包

在 Stage 2 现有 `apt-get update -qq` 后追加 `apt-get upgrade -y -qq`（或合并进同一 RUN），使 gzip/libpcre2/sqlite/perl 等系统包升级到 Debian trixie 修复版本（deb13u1/u2）。

注意：

- 必须放在 `apt-get purge` 之前（purge 只移除 python3-setuptools/python3-msgpack 两个 Debian 系统包，与 upgrade 无冲突）。
- `apt-get upgrade` 不引入新包（不装新依赖），体积增量小、风险低。
- 现有 RUN 已 `apt-get update -qq`，在同一层追加 `apt-get upgrade -yqq` 即可，保持层数不变。

### 改动 3：测试 — 消除假绿，锁定真实行为

`tests/test_static_path_traversal.py`：

- 穿越测试保持 `200/404` 皆可（破坏路径确实可能产生 fallback，语义不变）。
- 为「正常静态文件直出 / 未知路径 fallback」两个用例补充「路由必须注册」的前置断言语义：不依赖前置构建产物，改为在测试内构造 `static/` 目录后显式验证 `serve_spa` 行为（现状已然如此——monkeypatch 替换 `STATIC_DIR` 后请求），**无需改测试文件本身**（测试已正确 monkeypatch + 构造布局）。真正修复点是 main.py 使路由无条件注册；测试仅保持原样并依赖修复后行为转绿。

故本 change **不修改测试文件**，通过 main.py 修复让现有测试自然转绿；另在 CI 层补充一个回归防护（见改动 4）。

### 改动 4：CI — 复现与回归防护

选择最小方案：不加新 job、不加新步骤；修复后首个 main push 即全量回归（现状 ci.yml 的 pytest/trivy/前端构建已覆盖全部验收点）。pytest 在无 static 产物时即等价复现本 bug（当前 CI 已 5 次稳定复现），无需额外步骤。

若需更强的环境等价性，可选：pytest 步骤前预建空 `backend/static/index.html` 占位以强制「路由注册路径」被 CI 覆盖——但这是模拟产物补齐，属于掩盖而非修复；本 change 采用 main.py 无条件注册后，CI 的默认行为（无 static 产物）即覆盖「路由已注册 + fallback 兜底」两条路径，达到同样效果且更真实。

## 验收标准

1. `git ls-files backend/static` 为空（产物不入库）的前提下，CI `Backend 导入检查` 的 pytest 全部通过（含 `test_static_path_traversal.py` 3 用例）。
2. `镜像安全扫描（trivy）` job exit 0：扫描无 HIGH/CRITICAL 未豁免漏洞。
3. `前端类型检查与构建` 保持通过。
4. 本地同等复现：无 static 产物时 pytest `test_static_path_traversal.py` 全绿；有 static 产物时同样全绿（不回归既有行为）。