---
comet_change: remove-deprecated-settings
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-09-remove-deprecated-settings
status: final
---

# 废弃设置项清理技术设计（remove-deprecated-settings）

## 背景与目标

设置页「下载队列最大并发数（`download_queue_max_concurrent`）」已废弃：容量准入调度机制上线后该值不再生效，但前端仍展示、后端 GET 响应仍透传存量值。本 change 将其从设置页移除、停止透传，同时保留存量数据。

## 关键事实基线（已探查确认）

| 事实 | 位置 | 说明 |
|------|------|------|
| 前端条目 | `frontend/src/config/settingsMeta.ts:240-244` | 标签含「已废弃」，escape 后删除 |
| 后端引用 | 无（grep `*.py` 零匹配） | `config_store.py`、`routers/settings.py` 无读写/白名单/透传 |
| PATCH 白名单 | `_WHITELIST_EXACT` | 不含该键，无需改动 |
| 存量数据 | DB `system_config` 有 `download_queue_max_concurrent = "50"` | 保留不删（Non-Goal） |
| GET 行为 | `get_settings` 返回 system_config 全量行 | 无字段过滤 → 存量键仍出现在响应 |

## 设计决策

### D1：后端响应层排除（tasks 2.1）

`backend/app/routers/settings.py`：

- 新增模块级常量：

```python
# 已废弃设置键：DB 存量保留，但不再透传给前端（避免前端 fallback 展示英文键名）
_RETIRED_EXACT = {"download_queue_max_concurrent"}
```

- `get_settings` 构造 `config` 时跳过 `_RETIRED_EXACT` 中的键：

```python
for r in rows:
    if r.key in _RETIRED_EXACT:
        continue  # 已废弃键不透传（存量保留，不删）
    if config_store.is_sensitive(r.key):
        config[r.key] = "***"
    else:
        config[r.key] = r.value
```

- **不改**：`_WHITELIST_EXACT`（已不含该键）、`_EDITABLE_KEYS`（派生自白名单）、PATCH 逻辑、config_store 内部、数据库数据。

### D2：前端删除条目（tasks 1.1）

`frontend/src/config/settingsMeta.ts`：删除 `download_queue_max_concurrent` 条目（240-244 行）。`getSettingMeta` 的未知名回退逻辑不动（后端不再透传后不会命中该键）。

### D3：文档

- `docs/影视下载两队列重设计.md:293` 为历史设计文档说明（描述准入循环不再设该硬上限的演变），**保留原样**（OpenSpec spec 允许「文档说明」性提及）。
- README 已确认无提及。无需改动。

### D4：存量数据与测试

- 存量键值保留（Non-Goal）；`comet state` 无需迁移。
- 后端新增测试：`test_api_smoke.py` 或独立测试，构造 system_config 含存量键的场景，断言 `GET /api/settings` 响应的 `config`/`system_config` 均不含该键。
- 全量 pytest + 前端 `npm run build`（vue-tsc + vite build）。

## 风险与边界

- **与 media-poster-proxy 并发**：另一窗口同时修改 `backend/app/routers/settings.py`（其在 `_WHITELIST_EXACT` 加 `tmdb_poster_proxy`）。本 change 改的是 GET 响应构造与顶部常量，区域不重叠，行级无冲突；但必须**精确 `git add` 各自文件**，严禁 `git add -A` 双向污染。
- 不引入通用设置项生命周期框架（Non-Goal）。
- 回滚：`git revert` 即可，无数据迁移。

## 验证清单

1. `pytest tests/`（backend）全量通过
2. 新增断言：GET /api/settings 响应不含 `download_queue_max_concurrent`
3. `npm run build`（frontend）通过
4. grep 全仓：代码引用无残留（仅存量数据说明与历史文档提及）
