# Subagent 派发检查点 — emby-library-categories

## 当前 Task

- Plan task: Task 2: 后端按库查询条目（2.1 + 2.2）
- OpenSpec task: 2.1 services/emby.py 改造 `list_library(library_id, item_type, status)`；2.2 routers/emby.py 更新 `GET /api/emby/library`
- 阶段: `implementing`
- review_mode: standard
- model: fixer（后台）
- 风险信号: 公共 API 契约变更（/library 参数变更、移除 anime）→ 命中，需任务级 review

## 前序

- Task 1 complete（b43921e + 6b816ea，review clean）— 已勾选 1.1/1.2
- 已知过渡态：`list_library` anime 分支仍引用已删 `_find_anime_library_item_id`，本任务必须移除

## 证据

- BASE: 6b816ea
- RED/GREEN: 待 implementer 回报
- 提交: 待 implementer 回报

## 审查

- 每任务 reviewer: 待派发（命中风险信号：公共 API 契约变更）
- 审查-修复轮次: 0/1
- 反馈: 无

## 勾选

- plan checkbox: 未勾选
- openspec checkbox: 未勾选
