# 验证报告：remove-deprecated-settings

- Change: remove-deprecated-settings
- Date: 2026-09-09
- 验证模式: full（规模评估：6 任务 > 3、delta spec 1 能力、变更文件 13 > 8）
- 产物语言: zh-CN

## 摘要

| 维度 | 状态 |
|------|------|
| Completeness（完整性） | 6/6 任务完成、3 条 Requirement 覆盖 |
| Correctness（正确性） | 3/3 需求实现、3 场景覆盖 |
| Coherence（一致性） | Design Doc 决策全部落实，无漂移 |

## 检查项结果

### 1. tasks.md 全部任务已完成

- [x] 6/6 已勾选（1.1 / 2.1 / 2.2 / 3.1 / 4.1 / 4.2）
- 对应提交：`486a0f7`（后端响应层排除）、`8f1c54f`（前端移除条目）、`71f6c39`（勾选 tasks 记录证据）

### 2. 改动文件与 tasks.md 描述一致

使用 `git diff --stat 11d5051..HEAD`（限定本 change 路径）核对：

| 文件 | 变更 | 对应任务 |
|------|------|---------|
| `backend/app/routers/settings.py` | +8 行：新增 `_RETIRED_EXACT` 常量、`get_settings` 跳过逻辑 | 2.1 |
| `backend/tests/test_settings_retired.py` | 新增 84 行测试 | 2.1 / 4.2 |
| `frontend/src/config/settingsMeta.ts` | -5 行：删除废弃条目 | 1.1 |

与设计决策 D1（前端删除）、D2 修正版（后端响应层排除，见设计文档）一致；文档保留原样（D3，用户已确认）；无迁移（D4）。

### 3. 编译通过

- `npm run build`（frontend，vue-tsc + vite build）：PASS（Task 2 与 Task 3 两轮均通过）
- 提交 `f49e08a` 记录 build 证据：`pytest tests/ -q && npm run build` exit=0

### 4. 相关测试通过

- 新增 `tests/test_settings_retired.py`：1 passed（TDD 红→绿完整闭环：红=响应含存量键 `download_queue_max_concurrent: '50'`，绿=排除成功）
- 全量后端测试：**435 passed**（`pytest tests/ -q`，含既有 settings/smoke/config_store 回归）

### 5. 无明显安全问题

- `_RETIRED_EXACT` 为 frozenset 精确匹配，无前缀误伤；过滤在敏感键遮蔽之前，无泄漏路径（审查确认）
- PATCH 白名单不含该键，无法再写入（422），「不再写」天然成立
- 无硬编码密钥、无新增 unsafe 操作

### 6. 最终集成代码审查通过

- @oracle 独立审查（Approve）：无 CRITICAL / IMPORTANT 问题；4 条 SUGGESTION 全部非阻塞
- 与并行 change `media-poster-proxy` 同文件（settings.py）不同区域修改，审查确认无逻辑冲突、物理冲突概率低且语义正交

## 需求与场景覆盖对照

### Requirement: 废弃设置项从设置页移除
- **场景「设置页不含废弃项」**：`settingsMeta.ts` 条目已删（8f1c54f）；grep 无残留；`npm run build` 通过 → **覆盖**
- 后端 GET 不再透传，前端 `getSettingMeta` 不会回退展示英文键名（`_RETIRED_EXACT` 剔除） → **覆盖**

### Requirement: 废弃键不再参与读写与透传
- **场景「废弃键无代码引用」**：grep `*.py`/`*.ts`/`*.vue` 确认代码仅剩 `_RETIRED_EXACT` 定义与测试断言两处必要实现引用，无业务读取/写入 → **覆盖**
- 历史设计文档 `docs/影视下载两队列重设计.md:293` 提及保留（spec「文档说明」例外，用户已确认）→ **符合**

### Requirement: 存量数据不误伤
- **场景「存量键值保留」**：DB 查询确认 `system_config` 仍含 `download_queue_max_concurrent / 50`，未被删除，无迁移脚本 → **覆盖**

## 设计一致性（Coherence）

- Design Doc 决策 D1-D4 全部落实；`_RETIRED_EXACT` 常量命名/位置与设计文档一致
- 无 build 阶段 spec 增量修改，delta spec 与 design doc 无矛盾（无需 Spec 漂移处理）
- 不使用通用设置项生命周期框架（Non-Goal 遵守）

## 接受的偏差记录

| 级别 | 内容 | 原因与影响范围 |
|------|------|---------------|
| SUGGESTION | 测试文件末尾无换行符 | 非阻塞、纯格式；不影响 lint/CI（repo 内多个测试文件同为无尾换行惯例），接受 |
| SUGGESTION | 临时目录未清理（`mkdtemp`） | 与既有 `test_api_smoke.py` / `test_config_store.py` 惯例一致；仅 /tmp 累积测试临时目录，无功能影响，接受 |
| SUGGESTION | 可加「正常键仍返回」反向断言 | 可选回归增强；现有断言已覆盖核心验收场景，且 `_RETIRED_EXACT` 精确匹配无误伤面，接受 |
| SUGGESTION | config_store 缓存与 GET 一致性提示 | 信息性记录：GET 直接读 DB 行不读缓存，当前无泄漏路径；未来新增读取逻辑需同步过滤 `_RETIRED_EXACT`，接受 |

## 最终结论

**PASS** — 无 CRITICAL / IMPORTANT 问题。6 项检查全部 OK，可进入归档阶段。