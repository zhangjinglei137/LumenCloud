## Why

「搜索 → 转存 → 下载」全流程经多模型代码评审（council 三方共识）确认：主链路状态机、CAS 幂等与双路径完成推进设计扎实，但存在 1 项 P0、7 项 P1 及多项 P2 缺陷，集中在并发边界（事务内网络 IO 破坏原子段）、失败清理（夸克残留泄漏）、恢复兜底（GID 白名单自锁、节点计数被蚕食）与运维节流，长期运行会导致容量假性不足、下载静默停摆或任务被误杀。

## What Changes

- **P0-1** 准入事务内网络 IO：将容量检查移出准入事务（锁外 check → 重开短事务做 reserved 读 + CAS），快照落库移出 check 调用栈，消除 SQLite 单连接全局写阻塞与嵌套提交破坏原子段。
- **P1-1** webhook 推进 `scrape→library` 补齐节点字段重置（node_attempt=0 / node_finished_at / node_error），避免失败计数蚕食 recovery 有效重试次数。
- **P1-2** GID 白名单 fail-closed 增加逃生通道：白名单口径放宽为「DB 中 aria2_gid 非空全部行」，或对同一陌生 gid 连续跳过 N 轮后强制清理，打破孤儿任务自锁。
- **P1-3** NasTools webhook 单文件事件改为按文件名/集号推进单行，不再按 media_id 批量推进全部 scrape 行；无法定位时仅触发 library_check 轮询加速。
- **P1-4** cancel/skip 置终态后同事务同步 `_sync_media_status`，media 不再永久卡 downloading。
- **P1-5** `_commit_downloading` CAS 冲突分支补充 best-effort 清理已转存夸克文件（`final_quark_path` 传入），消除空间累积泄漏。
- **P1-6** 取件 CAS 后 INSERT 撞 UNIQUE 改为原子化条件 INSERT（SELECT NOT EXISTS），TaskQueue 源行不再误标 done。
- **P1-7** 容量记账漏计窗口：downloading 已落盘文件单列账本或准入提交强制失效 used 缓存，杜绝突破容量硬上限。
- **P2** 刮削连坐风暴指数退避；下载重试清理本地半成品/传覆盖选项（需确认 aria2 参数）；集级确认 fail-open 延迟复核；nastools 失败通知节流；`_alert_cooldown` 多 worker 节流落库；quota_wait 唤醒-置回写放大优化；凭据不完整不建 DQ；`_node_failure` 冲突分支条件化清 save_task_id；NaSTools token 移除 `?token=` query 通道。

## Capabilities

### New Capabilities

（无新增 capability）

### Modified Capabilities

- `pipeline-admission`（`docs/openspec/specs/pipeline-admission`）：容量判断与等待队列要求变更——容量检查不得在准入事务内执行网络 IO（P0-1）；容量记账口径覆盖 downloading 已落盘文件（P1-7）；GID 来源校验提供孤儿任务逃生通道（P1-2）。
- `pipeline-transfer`（`docs/openspec/specs/pipeline-transfer`）：转移整理与入库确认要求变更——webhook 推进按文件级单行推进并重置节点字段（P1-1/P1-3）；转存链 CAS 冲突清理夸克残留（P1-5）；取件与 DQ 创建原子化（P1-6）；cancel/skip 同步 media 状态（P1-4）。

## Impact

- **后端代码**：`backend/app/tasks/transfer.py`（准入事务、转存链、CAS 提交、轮询）、`backend/app/tasks/library_check.py`（刮削执行、入库确认、集级判定）、`backend/app/tasks/nastools_sync.py`（同步/通知）、`backend/app/routers/queue.py`（cancel/skip、进度端点）、`backend/app/routers/nastools_notify.py`（webhook 推进）、`backend/app/tasks/scan.py`（全量模式防重）、`backend/app/tasks/recovery.py`（超时回退）、`backend/app/services/capacity.py`（容量检查/快照）、`backend/app/services/aria2.py`（add_uri 选项）。
- **API**：无 public API 变更；webhook 行为语义修正（内部契约）。
- **测试**：`backend/tests/` 对应模块新增/更新回归测试（准入事务边界、webhook 推进、GID 逃生、容量记账、取件原子化）。
- **部署/运维**：需确认 aria2 启动参数（allow-overwrite）与 worker 数量，影响 P2 两项处置。
