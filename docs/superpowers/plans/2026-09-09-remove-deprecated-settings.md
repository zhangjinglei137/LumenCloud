---
change: remove-deprecated-settings
design-doc: docs/superpowers/specs/2026-09-09-remove-deprecated-settings-design.md
base-ref: 11d5051725a57ab858eee276c86c22d92e249c34
---

# 废弃设置项清理实施计划（remove-deprecated-settings）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已废弃的「下载队列最大并发数（`download_queue_max_concurrent`）」从设置页移除并停止后端透传，同时保留 system_config 存量键值。

**Architecture:** 后端 `GET /api/settings` 返回 system_config 全量行（无字段过滤），需在路由响应构造处增加已废弃键集合 `_RETIRED_EXACT` 并跳过；前端 `settingsMeta.ts` 删除对应条目。前端 `getSettingMeta` 未知名回退逻辑不动（后端不再透传后不会命中该键）。

**Tech Stack:** FastAPI / SQLAlchemy async / pytest；Vue 3 / TypeScript / Vite 6。

**Spec:**
- `docs/openspec/changes/remove-deprecated-settings/specs/settings-lifecycle/spec.md`
- 深度设计：`docs/superpowers/specs/2026-09-09-remove-deprecated-settings-design.md`

## Global Constraints

- **产物语言**：commit message 使用中文，遵循 Conventional Commits（`<type>(<scope>): <中文摘要>`），摘要 50 字符内、动词开头、不写句号。
- **TDD**：后端任务先写失败测试（红）→ 运行确认失败 → 实现（绿）→ 运行确认通过 → 提交。
- **后端运行环境**：`backend` 目录下运行 pytest（`python -m pytest tests/test_settings_retired.py -v`）；不依赖 localhost 演练进程。
- **前端验证**：`npm run build`（= `vue-tsc --noEmit && vite build`）须通过。
- **并行窗口约束**：另一窗口正在执行 change `media-poster-proxy`，会同时修改 `backend/app/routers/settings.py`（`_WHITELIST_EXACT` 加 `tmdb_poster_proxy`）。本 change 只改**同一文件的 GET 响应构造与顶部常量区域**，互不重叠；git 提交必须**精确 add 本 change 文件**，严禁 `git add -A`。
- **存量数据**：不删 `system_config` 存量键值、不新增迁移、不改 config_store 内部。
- **文档**：`docs/影视下载两队列重设计.md:293` 历史设计文档提及**保留原样**（用户已确认）。

---

### Task 1: 后端响应层排除废弃键（tasks.md 2.1）

**Files:**
- Modify: `backend/app/routers/settings.py`（顶部常量区 + `get_settings` 响应构造 L133-139）
- Create: `backend/tests/test_settings_retired.py`

**Interfaces:**
- Consumes: `SystemConfig`（key 为 PK，`session.merge` 可 UPSERT）、`get_settings` 现有返回结构 `{"system_config", "config", "services", "editable_keys"}`
- Produces: 模块常量 `_RETIRED_EXACT: frozenset[str]`（后续无消费者，语义自包含）；`GET /api/settings` 的 `system_config` 与 `config` 均不含 `download_queue_max_concurrent`

- [x] **Step 1: 写失败测试 `backend/tests/test_settings_retired.py`**

```python
"""废弃设置键不透传单测（remove-deprecated-settings）。

GET /api/settings 返回 system_config 全量行；已废弃键（download_queue_max_
concurrent）DB 存量保留，但响应层排除，避免前端 fallback 展示英文键名。
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lumencloud_settings_retired_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP
# Phase 8：JWT 密钥自动文件化；env 值仅用于 settings 面板 jwt_secret 布尔断言
os.environ["JWT_SECRET"] = "retired-secret-12345"
# 隔离外部服务：显式置空，避免真实网络调用（与 test_api_smoke.py 同模式）
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""
os.environ["ALIST_BASE_URL"] = ""
os.environ["ALIST_TOKEN"] = ""
os.environ["ARIA2_RPC_URL"] = ""
os.environ["ARIA2_TOKEN"] = ""
os.environ["NASTOOLS_BASE_URL"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_USERNAME"] = ""
os.environ["CLOUDSAVER_PASSWORD"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _seed_retired_key():
    """注入存量废弃键（同事件循环），模拟历史 system_config 数据。"""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.database import async_session
    from app.models import SystemConfig

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        session.merge(SystemConfig(key="download_queue_max_concurrent", value="50", updated_at=now))
        await session.commit()
        result = await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == "download_queue_max_concurrent")
        )
        return result.scalars().first()


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。"""
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import User
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None  # 已删除 admin，必然重新创建并返回初始密码
    return password


def test_settings_response_excludes_retired_key():
    with TestClient(app) as client:
        # 存量废弃键注入成功（模拟 DB 中确有历史数据）
        assert client.portal.call(_seed_retired_key) == "50"

        # admin 登录
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        admin_tok = r.json()["access_token"]

        r = client.get("/api/settings", headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200
        data = r.json()
        # 废弃键既不出现 system_config 也不出现 config（前端契约键）
        assert "download_queue_max_concurrent" not in data["system_config"]
        assert "download_queue_max_concurrent" not in data["config"]
```

- [x] **Step 2: 运行确认失败**

Run: `python -m pytest tests/test_settings_retired.py -v`（workdir: `backend`）
Expected: FAIL — `assert "download_queue_max_concurrent" not in data["system_config"]`（当前 `get_settings` 全量返回存量行）

- [x] **Step 3: 实现 `settings.py` 响应层排除**

`backend/app/routers/settings.py`，`_EDITABLE_KEYS` 定义后新增：

```python
# 已废弃设置键：system_config 存量保留，但 GET 响应不再透传（避免前端
# fallback 展示英文键名）。PATCH 白名单（_WHITELIST_EXACT）本就不含这些键。
_RETIRED_EXACT = frozenset({"download_queue_max_concurrent"})
```

`get_settings` 中构造 config 的循环改为：

```python
    for r in rows:
        if r.key in _RETIRED_EXACT:
            continue  # 已废弃键不透传（存量保留，不删）
        if config_store.is_sensitive(r.key):
            config[r.key] = "***"  # 敏感键不回显值（占位）
        else:
            config[r.key] = r.value
```

- [x] **Step 4: 运行确认通过**

Run: `python -m pytest tests/test_settings_retired.py -v`
Expected: PASS（后端既有 `test_api_smoke.py` / `test_config_store.py` 不受影响）

- [x] **Step 5: 精确提交（并行窗口约束）**

```bash
git add backend/app/routers/settings.py backend/tests/test_settings_retired.py
git commit -m "feat(settings): 响应层排除已废弃键 download_queue_max_concurrent"
```

---

### Task 2: 前端移除废弃设置项（tasks.md 1.1）

**Files:**
- Modify: `frontend/src/config/settingsMeta.ts`（删除 L240-244 条目）

**Interfaces:**
- Consumes: 无
- Produces: `SETTING_FIELD_META` 不再含 `download_queue_max_concurrent`；`getSettingMeta` 签名与回退逻辑不变

- [x] **Step 1: 删除条目**

`frontend/src/config/settingsMeta.ts` 中删除：

```ts
  download_queue_max_concurrent: {
    label: '下载队列最大并发数（已废弃）',
    desc: '遗留配置：旧版下载队列的并发上限。当前版本已改为容量准入调度机制，该值不再生效，无需修改；仅为兼容历史数据保留，后续版本可能移除。',
    default: '已废弃，不再生效',
  },
```

注意：`download_queue_paused`（保留项，L230-239）与 `scrape_revert_timeout_hours`（L245）之间删除该块，保留 `// ---------- 下载队列 ----------` 注释。

- [x] **Step 2: 验证设置页不再渲染该条目**

Run: `grep -n "download_queue_max_concurrent\|下载队列最大并发数" frontend/src/config/settingsMeta.ts`
Expected: 无输出（0 匹配，settingsMeta.ts 中无残留）

- [x] **Step 3: 前端构建通过**

Run: `npm run build`（workdir: `frontend`）
Expected: 构建成功（`vue-tsc --noEmit` 无类型错误 + `vite build` 成功，产物写入 `backend/static`）

- [x] **Step 4: 精确提交（并行窗口约束）**

```bash
git add frontend/src/config/settingsMeta.ts
git commit -m "refactor(settings): 移除已废弃下载队列最大并发数设置项"
```

---

### Task 3: 全量验证收尾（tasks.md 2.2 + 3.1 + 4.1 + 4.2）

**Files:**
- 验证用：`backend/tests/`（全量）、`frontend`（build）
- 只读核对：`docs/影视下载两队列重设计.md`、README、数据库 `system_config`

**Interfaces:**
- Consumes: Task 1/2 全部产物
- Produces: 通过证据（测试输出、构建产物、grep 结果）

- [x] **Step 1: grep 全仓验证无残留代码引用（tasks 2.2）**

Run: `grep -rn "download_queue_max_concurrent" --include="*.py" --include="*.ts" --include="*.vue" .`
Expected: 仅剩 `docs/openspec/changes/remove-deprecated-settings/`（change 产物）与 `docs/影视下载两队列重设计.md:293`（历史设计文档，保留决策）。代码零引用。

- [x] **Step 2: 文档检索确认（tasks 3.1）**

Run: `grep -rn "下载队列最大并发数" README.md docs/ 2>/dev/null | grep -v "openspec/changes/remove-deprecated-settings" | grep -v "superpowers"`
Expected: 仅 `docs/影视下载两队列重设计.md:293` 一处历史设计文档提及（保留原样，用户已确认）。

- [x] **Step 3: 存量数据确认（tasks 4.1）**

Run: `sqlite3 backend/data/lumencloud.db "SELECT key, value FROM system_config WHERE key='download_queue_max_concurrent';"`
> 若测试库路径不同，改用 MCP `lumencloud_db_mcp_readQuery`：`SELECT key, value, updated_at FROM system_config WHERE key = 'download_queue_max_concurrent'`
Expected: 返回存量行（当前为 `download_queue_max_concurrent / 50`），键值保留未被删除。

- [x] **Step 4: 全量后端测试（tasks 4.2）**

Run: `python -m pytest tests/ -q`（workdir: `backend`）
Expected: 全部 PASS。若存在与本次 change 无关的 pre-existing 失败，记录失败用例名与根因（归因），不视为本 change 回归。

- [x] **Step 5: 前端构建（tasks 4.2）**

Run: `npm run build`（workdir: `frontend`）
Expected: 构建成功（重复 Task 2 Step 3 确认最终状态）。

- [x] **Step 6: 确认 tasks.md 全勾选**

按 `docs/openspec/changes/remove-deprecated-settings/tasks.md` 勾选全部 6 个任务（1.1-4.2），随后将完成证据交回 Comet Build 流程（guard build --apply）。