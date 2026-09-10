# Brainstorm Summary

- Change: queue-inspection-rework
- Date: 2026-09-10

## 已确认的技术方案

### A. 后端队列列表（backend/app/routers/queue.py）
- `_list_flat` 排序改为创建时间升序（TQ 用 `created_at ASC`，DQ 用 `enqueued_at ASC`），统一键为「创建时间」
- `_list_download` 排序改为 `enqueued_at ASC, id ASC`
- 巡检队列（`_list_flat`）行补字段：`media_title`（join media.title）、`episode`、`share_code`（明文，仅 admin）、`status`、`file_size`（真实）、`updated_at`
- 下载队列（`_list_download`）补 `share_url`：夸克分享地址 `https://pan.quark.cn/s/<share_code>`，域名集中常量；无 share_code 返回 null
- 分页契约：接口返回 `{items, total}` 供 el-pagination 使用；后端保留 limit/offset 参数
- 顺带修复越权口子：guest 一律不返回 share_code（含 `_list_download`），admin 明文完整 share_code
- 排序/分页下沉 SQL（`_list_flat` 当前是全量拉取内存切片，改为 SQL 排序 + limit/offset + 计数）

### B. 入库确认卡死修复（backend/app/tasks/library_check.py）
- 根因：Emby 命中但遗漏集判定 continue 的分支不经过超时判定，若 Emby 一直返回该集在遗漏集则永不 finalize
- 修复：遗漏集 continue 分支纳入超时窗口（超时则按超时处理并记日志）；各 continue 分支（缺 tmdb_id / find_emby_id 异常 / get_missing 异常 / 遗漏集）补明确日志记录不 finalize 的具体原因；超时判定覆盖「Emby 命中但遗漏集判定」路径
- scheduler 注册已确认存在（30s 周期），无需新增注册

### C. 大小真实值（file_size）
- 根因：`scan.py _walk_share`（:650-660）对 size_unknown 文件按「分享总大小/文件数」均摊估算 → 同分享所有行同值
- 展示策略：展示本行数据库 file_size，估算来源前端标注「约」前缀；调用方感知估算标记（scan 已打 `size_estimated`，需沿 DQ/TQ 行输出或前端按 share 级推断，设计 Doc 定）
- 数据回填：下载完成后用 aria2 真实大小回填 `DownloadQueue.file_size`（transfer.py `_complete_download` 处），逐步逼近真实值
- 无值行显示「—」

### D. 前端（frontend/src/views/QueueView.vue + stores/queue.ts + api + types）
- Tab「任务队列」→「巡检队列」
- 巡检队列行展示：影视名称 / SxxExx / 分享码明文（admin）/ 状态 / 真实大小（约标注）/ 更新时间
- 下载队列分享码明文展示且可点击跳转 `share_url`（无地址不可点击）
- 改 el-pagination 标准分页（page/pageSize/total），移除 loadMore「加载更多」交互
- store/pageSize 契约同步（total/page 字段、share_url、media_title）
- 新建前端测试（QueueView 渲染 / queue store 分页数据流 / 分享码链接 / format 约标注）

## 关键取舍与风险
- 分享码明文（admin）与 §9.1 脱敏冲突 → 决策：admin 明文、guest 不返回，回写 delta spec 明确权限边界
- 估算值标注「约」而非隐藏 → 保留信息量；aria2 回填逐步真实化
- `_list_flat` 全量内存切片 → SQL 化（分页总行数与排序一致性）
- 卡死修复依赖真实 Emby 环境 → 单测覆盖判定逻辑 + 部署后观察日志

## 测试策略
- 后端 pytest：排序断言（多行不同 created_at/enqueued_at）、admin/guest share_code 差异、share_url 构造、遗漏集终态、超时路径
- 前端 vitest：队列行字段渲染、分享码链接、分页数据流、约标注

## Spec Patch
1. 分享码明文权限边界：delta spec 明确「admin 明文完整 share_code，guest 不返回」，覆盖 §9.1 例外
2. 大小展示：估算值标注「约」（场景补充：估算来源显示约前缀；下载完成后回填真实值）
3. 入库确认：遗漏集路径纳入超时（场景补充：Emby 命中但持续在遗漏集 → 超时不无限等待）