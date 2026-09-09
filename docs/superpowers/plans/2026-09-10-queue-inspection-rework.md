# queue-inspection-rework 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 巡检队列（原任务队列）与下载队列完成展示重构（改名、字段补全、真实大小、分页排序、分享码明文可跳转、去 loadMore）并修复入库确认卡死。

**Architecture:** 后端 `queue.py` 改排序/字段/分页契约（`{items,total}`）+ admin/guest 脱敏；`library_check.py` 把遗漏集 continue 分支纳入超时窗口；`scan.py`/`transfer.py` 打通 `size_estimated` 落库与 aria2 真实大小回填。前端 QueueView 改 el-pagination、分享码明文链接、Tab 改名、约标注。

**Tech Stack:** Python FastAPI + SQLAlchemy + Alembic / Vue3 + Pinia + Element Plus / pytest / vitest

**Spec:** docs/openspec/changes/queue-inspection-rework/specs/queue-inspection-display/spec.md
**Design Doc:** docs/superpowers/specs/2026-09-10-queue-inspection-rework-design.md

---
change: queue-inspection-rework
design-doc: docs/superpowers/specs/2026-09-10-queue-inspection-rework-design.md
base-ref: 4a4682d9796a209f4aafc71d0fe11e118f9de1e6
---

## Global Constraints

- 产物语言：zh-CN（日志、commit message、代码注释用中文）
- 脱敏：share_code/share_url 仅 admin 返回明文；guest 一律不返回（修复现有 `_list_download` 越权口子，与 §9.1 对齐）
- share_url 域名集中常量：`https://pan.quark.cn/s/<share_code>`；无 share_code → null
- 排序：巡检队列（`_list_flat`）TQ 按 `created_at ASC, id ASC`、DQ 按 `enqueued_at ASC, id ASC`（DQ 无 created_at 列，enqueued_at 即创建时间）；下载队列（`_list_download`）`enqueued_at ASC, id ASC`
- 分页契约：`GET /queue` 与 `GET /queue?type=download` 返回 `{"items": [...], "total": N}`（保留 limit/offset 入参）
- 大小：`size_estimated=true` 的行前端展示「约 X」；`file_size` 为空显示「—」；下载完成用 aria2 totalLength 回填真实值并清 size_estimated
- 迁移：TaskQueue/DownloadQueue 新增 `size_estimated` 布尔列（nullable，default null，历史行=未知按非估算处理 → 历史数据不标「约」）
- 测试命令：后端 `cd backend && pytest`；前端 `cd frontend && npm run test`（vitest）

**已完成的调查结论（tasks 2.1 / 3.1 根因，直接采用，不需要再调查）：**

1. **大小同值根因**：`scan.py _walk_share:650-660` —— cloudSaver share-list 不返回单文件 size，对 `size_unknown` 文件按「分享总大小 / 文件数」均摊估算，同分享所有行同值。`_walk_share` 已在返回 dict 打 `size_estimated` 标记，但 `_enqueue` 未落库 → 需要把标记写入 TaskQueue；取件/手动 promote 拷贝到 DownloadQueue；aria2 下载完成后回填真实 totalLength。
2. **入库确认卡死根因**：`library_check.py:342-365` —— Emby 命中后若 `_episode_in_missing` 持续为 True（追更新集长期在遗漏集），`continue` 分支**不经过 `_mark_timeout_if_expired` 超时判定**，永不 finalize。`media.tmdb_id is None` 分支（:333-335）同样不消耗超时。二者必须纳入超时窗口。

---

### Task 1: 后端分页契约 + 排序 + 分享码字段（queue.py 路由层）

**Files:**
- Modify: `backend/app/routers/queue.py:91-217`
- Test: `backend/tests/test_queue_list.py`（新建）

**Interfaces:**
- Consumes: `_list_flat(session, limit, offset, is_admin)` / `_list_download(session, limit, offset, is_admin)` 签名修订；`User.role`（'admin'|'guest'）
- Produces: `GET /queue` 返回 `{"items": [...], "total": N}`；`_list_flat` 行新增 `share_code`（admin 明文）、`size_estimated`；`_list_download` 行新增 `share_url`、`size_estimated`

- [x] **Step 1: 写失败测试**（新建 `backend/tests/test_queue_list.py`）

```python
"""queue 列表接口契约测试：分页 total、排序、脱敏、share_url。

基建：完全参照 backend/tests/test_media_two_queue.py —— 模块导入前把
LUMENCLOUD_DATA_DIR 指向临时目录 + 隔离全部外部服务环境变量 + TestClient(app) +
登录 admin/guest 拿 token（_auth helper）+ async_session 直接 seed 数据。

用例（实现时用真实 seed 数据替代下述伪代码中的 …）：
1. test_list_queue_returns_items_and_total —— seed 3 条活跃行，GET /queue
   断言返回 dict 且 items 长度=limit、total==3；limit=1 时 items 长度=1 而 total 仍=3
2. test_list_flat_sorts_by_created_asc_and_id_asc —— seed 多行不同 created_at/enqueued_at，
   断言返回顺序为创建时间从小到大、同时间 id 从小到大
3. test_list_download_sorts_by_enqueued_asc —— ?type=download 断言 enqueued_at 升序
4. test_share_code_admin_plain_guest_hidden —— admin token 响应含明文 share_code；
   guest token 响应不含/为 null（download 与 flat 两种都验证）
5. test_share_url_constructed —— seed 有 share_code 的 DQ 行，admin 响应
   share_url == "https://pan.quark.cn/s/<code>"；无 share_code 行 share_url 为 null
```

- [x] **Step 2: 运行确认失败**

Run: `cd backend && pytest tests/test_queue_list.py -v`
Expected: FAIL（返回结构断言不匹配，仍是裸数组）

- [x] **Step 3: 实现**

`backend/app/routers/queue.py`：

```python
# 模块顶部新增常量（share_url 域名集中一处）
_QUARK_SHARE_URL = "https://pan.quark.cn/s"


def _share_url(code: str | None) -> str | None:
    """夸克分享地址；无分享码返回 None（前端据此降级为不可点击）。"""
    if not code:
        return None
    return f"{_QUARK_SHARE_URL}/{code}"
```

修订 `_list_flat`（91-168）：
- 签名加 `is_admin: bool`
- 排序：TQ 查询改 `order_by(TaskQueue.created_at.asc(), TaskQueue.id.asc())`；DQ 查询改 `order_by(DownloadQueue.enqueued_at.asc(), DownloadQueue.id.asc())`
- 合并排序键改升序：`rows.sort(key=lambda r: (r["enqueued_at"] or "", r["id"]))`（TQ 行 enqueued_at 已映射为 created_at，:149；DQ 行为 enqueued_at，:163 —— 统一该键即「创建时间升序」）
- total：切片前 `total = len(rows)`；返回 `{"items": rows[offset : offset + limit], "total": total}`
- 行字段：`_list_flat` 的 TQ/DQ 两个 dict 补 `"share_code": tq.share_code if is_admin else None`（DQ 同理）、`"size_estimated": bool(tq.size_estimated)`（DQ 同理）
- 保持现有 `title`（=join media.title，满足「影视名称」展示，不再另加 media_title 冗余字段）

修订 `_list_download`（171-203）：
- 签名加 `is_admin: bool`；排序改 `enqueued_at.asc(), id.asc()`
- 加 count 查询：`total = (await session.execute(select(func.count()).select_from(DownloadQueue).where(DownloadQueue.status.in_(_DQ_ACTIVE)))).scalar_one()`（SQL 查询改为不带 limit/offset 的同 where 条件 count）
- 行字段：`"share_code": dq.share_code if is_admin else None`、`"share_url": _share_url(dq.share_code) if is_admin else None`、`"size_estimated": bool(dq.size_estimated)`
- 返回 `{"items": result, "total": total}`

修订 `list_queue`（206-217）：

```python
@router.get("/queue")
async def list_queue(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    type: Annotated[Optional[str], Query()] = None,
) -> dict:
    """扁平任务列表或 ?type=download 下载队列（{items, total} 分页契约）。"""
    is_admin = user.role == "admin"
    if type == "download":
        return await _list_download(session, limit, offset, is_admin)
    return await _list_flat(session, limit, offset, is_admin)
```

- [x] **Step 4: 运行通过**

Run: `cd backend && pytest tests/test_queue_list.py tests/test_media_two_queue.py -v`
Expected: 新增测试 PASS；既有 queue/media 测试不回归

- [x] **Step 5: 勾选 OpenSpec 任务并提交**

```
勾选 tasks.md 1.1 / 1.2 / 1.3 / 1.4 / 1.5
git add backend/app/routers/queue.py backend/tests/test_queue_list.py docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "feat(queue)：巡检/下载队列分页契约、创建时间升序、分享码脱敏与 share_url
- GET /queue 返回 {items,total}，_list_flat/_list_download 改为创建时间升序
- share_code/share_url 仅 admin 返回明文，guest 脱敏（修复 guest 越权口径）
- 补 size_estimated 字段输出（供前端约标注）"
```

**注意（Task 1 已执行交付记录）：** 该 commit 实际由 Task 1 implementer 以 `27edc97` 创建（仅 queue.py + test_queue_list.py，不含 tasks.md 勾选——勾选由协调者在审查通过后完成）。若重新执行本任务不得重复提交同段。

---

### Task 1b: 既有测试旧契约断言同步（test_queue.py / test_scan_run_phases.py）

**背景（Task 1 契约变更的必要清理，found by Task 1 implementer）：**
Task 1 将 `GET /queue` 返回从裸数组改为 `{items, total}`、行字段新增 `share_code`/`share_url`/`size_estimated`（guest 视角 null）、`_list_download` 新增活跃态过滤（total/items 同口径）。既有测试仍断言旧契约：

- `backend/tests/test_queue.py`：`test_list_flat_union_dq_tq_with_fields`、`test_list_flat_excludes_terminal_states`、`test_list_flat_dedup_promoted_snapshot`、`test_list_flat_contract_fields_and_no_credentials`（断言恰 10 字段定界且 share_code 不在行内）、`test_list_download_flat_type_download`（断言终态 done 行仍返回）→ 6 用例直接调用 `list_queue(user=..., session=...)` 裸拿数组
- `backend/tests/test_scan_run_phases.py`：`test_scan_queue_has_no_tree_fields` 一带调用 `await list_queue(user=MagicMock(), session=s, limit=100, offset=0)` 直接遍历裸数组

**Files:**
- Modify: `backend/tests/test_queue.py`（6 处调用点 + 字段定界断言 + 新增 admin/guest 脱敏用例）、`backend/tests/test_scan_run_phases.py`（1 处调用点）
- Test: 复用既有文件，无需新增文件

**Interfaces:**
- Consumes: Task 1 新契约 `{items, total}`、行字段 `share_code`/`share_url`/`size_estimated`（admin 明文 / guest null）、`_list_download` 活跃态过滤（终态不返回）
- Produces: 全量 `pytest` 绿色；既有队列测试语义与新契约一致

- [ ] **Step 1: 改 `test_queue.py` 六处断言**

模式：`rows = run(_case())` → `res = run(_case()); rows = res["items"]`；并在合适处补 `assert res["total"] == N`（N 为该用例活跃行数）：
- `test_list_flat_union_dq_tq_with_fields`：3 行 `res["total"] == 3`
- `test_list_flat_excludes_terminal_states`：活跃 8 行 `res["total"] == 8`
- `test_list_flat_dedup_promoted_snapshot`：`res["total"] == 1`
- `test_list_flat_contract_fields_and_no_credentials`：字段集加 `share_code`/`size_estimated`（admin 明文断言的另一用例）；若该用例传 `user=_admin()`，断言 `share_code == 明文`（去掉「share_code 不在行内」断言，改为「guest 不返回」用例负责）
- `test_list_download_flat_type_download`：`res["total"]`；**终态 done 行不再返回** → 期望改为仅 downloading 行 + `res["total"] == 1`（并把原「终态仍返回」语义断言删除/翻转）

新增用例 `test_list_share_code_admin_vs_guest`（沿用本文件 `_admin()`/`_guest()` helper 与 `db`/`env` fixture）：
- seed DQ（share_code 12 位）+ TQ ready 行
- admin 调用 `list_queue(...)`：flat 与 download 两态均见明文 share_code；download 态见 share_url == `https://pan.quark.cn/s/<code>`
- guest 调用：share_code / share_url 均为 None（两态皆验）

- [ ] **Step 2: 改 `test_scan_run_phase.py` 调用处**

`rows = await list_queue(user=MagicMock(), session=s, limit=100, offset=0)` → `res = await list_queue(...); rows = res["items"]`；`by_id = {r["media_id"]: r for r in rows}` 遍历保持；补充 `res["total"]` 数量断言（按其 seed 活跃行数）。

- [ ] **Step 3: 运行定向**

Run: `cd backend && pytest tests/test_queue.py tests/test_scan_run_phases.py tests/test_queue_list.py -v`
Expected: 全通过

- [ ] **Step 4: 全量后端回归**

Run: `cd backend && pytest`
Expected: 全通过（含 Task 1 新增 test_queue_list.py）

- [ ] **Step 5: 勾选并提交**

```
勾选 tasks.md 1.6
git add backend/tests/test_queue.py backend/tests/test_scan_run_phases.py docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "test(queue)：既有测试同步新契约 {items,total} 与 share_code 明文
- test_queue.py 六处改读 res['items']/res['total']，字段定界含 share_code/size_estimated
- test_list_download_flat 终态行改为不返回（活跃态过滤），补 admin/guest 脱敏用例
- test_scan_run_phases 调用处同步新契约"
```

---

### Task 2: size_estimated 落库 + aria2 真实大小回填（数据链路）

**Files:**
- Modify: `backend/alembic/versions/0016_queue_size_estimated.py`（新建）、`backend/app/models/__init__.py`（TaskQueue/DownloadQueue 加列）、`backend/app/tasks/scan.py:953-997`、`backend/app/tasks/transfer.py:1314-1335`（取件拷贝）、`backend/app/routers/queue.py:624-633`（promote 拷贝）、`backend/app/tasks/transfer.py:526-567`（`_complete_download` 回填）、`backend/app/tasks/transfer.py:1585-1639`（`trigger_download_complete` 回填）
- Test: `backend/tests/test_queue_size_estimated.py`（新建）

**Interfaces:**
- Consumes: `scan._walk_share` 返回 dict 的 `size_estimated` key；`aria2.client.tell_status(gid)['totalLength']`
- Produces: `TaskQueue.size_estimated` / `DownloadQueue.size_estimated` 列；`_complete_download(..., real_size: int | None = None)` 新增可选参数

- [ ] **Step 1: 写迁移**（新建 `backend/alembic/versions/0016_queue_size_estimated.py`）

```python
"""Add size_estimated to task_queue / download_queue.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_queue", sa.Column("size_estimated", sa.Boolean(), nullable=True))
    op.add_column("download_queue", sa.Column("size_estimated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("download_queue", "size_estimated")
    op.drop_column("task_queue", "size_estimated")
```

- [ ] **Step 2: 模型加列**（`backend/app/models/__init__.py`）

`TaskQueue`（115-152 区）：`file_size` 后加：

```python
    size_estimated = mapped_column(Boolean)  # file_size 为均摊估算值（cloudSaver 无单文件 size）
```

`DownloadQueue`（155-209 区）：`file_size` 后加同款列。

- [ ] **Step 3: 写入路径打通**

a) `scan.py _enqueue`（953-997）签名加 `size_estimated: bool = False`，写入 `size_estimated=size_estimated`；调用点（约 :1538 `_enqueue(...)`）在 `file_size=int(f.get("file_size") or 0)` 处补 `size_estimated=bool(f.get("size_estimated"))` 实参（f 来自 `_walk_share`，已带该标记）。

b) `transfer.py _fetch_from_task_queue`（1314-1335 取件生成 DQ 处）：`DownloadQueue(...)` 构造补 `size_estimated=r.size_estimated`。

c) `queue.py promote_task`（624-633 手动 promote 生成 DQ 处）：构造补 `size_estimated=tq.size_estimated`。

- [ ] **Step 4: aria2 真实值回填**（`transfer.py`）

`_complete_download`（526）签名加 `real_size: int | None = None`，update `.values(...)` 补：

```python
                    file_size=real_size,
                    size_estimated=False,
```

（当 real_size 为 None 时不覆盖——SQLAlchemy 传 None 会写 NULL，故改为条件构造：仅当 `real_size` 非 None 时 put 这两个 values。实现：先构造 `vals = {status, node_attempt, ...}`，`if real_size: vals.update(file_size=real_size, size_estimated=False)`，再 `.values(**vals)`。但 file_size 为 NOT NULL，写 NULL 会失败——必须用 if 分支。）

调用点 `_poll_downloading_tasks`（473-475）：

```python
        if status == "complete":
            try:
                real_size = int((st or {}).get("totalLength") or 0) or None
            except (TypeError, ValueError):
                real_size = None
            await _complete_download(dq_id, media_id, episode, file_name, quark_path,
                                     retry_c, node_attempt, real_size)
```

`trigger_download_complete`（1585-1639）：update 前取真实大小：

```python
                real_size: int | None = None
                try:
                    st = await aria2.client.tell_status(gid)
                    real_size = int((st or {}).get("totalLength") or 0) or None
                except Exception:
                    pass  # aria2 查询失败 → 不回填，保持估算值
```

update `.values(...)` 同样条件化追加 `file_size=real_size, size_estimated=False`。

- [ ] **Step 5: 写并运行测试**（`backend/tests/test_queue_size_estimated.py`）

```python
"""size_estimated 落库 / 拷贝 / aria2 回填链路测试。"""
# 用例：
# 1. scan._enqueue 带 size_estimated=True 后 TaskQueue 行 size_estimated=True
# 2. 取件（_fetch_from_task_queue）生成的 DQ.size_estimated 与 TQ 一致
# 3. _complete_download(real_size=123) 后 DQ.file_size==123 且 size_estimated=False
# 4. _complete_download(real_size=None) 不改 file_size
# 5. 迁移 upgrade/downgrade 冒烟（alembic upgrade head 可执行）
```

Run: `cd backend && pytest tests/test_queue_size_estimated.py tests/test_transfer.py tests/test_council_fixes.py -v`
Expected: 新用例 PASS，既有 transfer 测试不回归

- [ ] **Step 6: 勾选 OpenSpec 任务并提交**

```
勾选 tasks.md 2.1 / 2.2
git add backend/alembic/versions/0016_queue_size_estimated.py backend/app/models/__init__.py backend/app/tasks/scan.py backend/app/tasks/transfer.py backend/app/routers/queue.py backend/tests/test_queue_size_estimated.py docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "feat(queue)：size_estimated 落库与 aria2 真实大小回填
- task_queue/download_queue 新增 size_estimated 布尔列（迁移 0016）
- 探测入队/取件/promote 全链路拷贝估算标记
- 下载完成（轮询+回调）用 aria2 totalLength 回填真实 file_size 并清估算标记"
```

---

### Task 3: 入库确认卡死修复（library_check.py）

**Files:**
- Modify: `backend/app/tasks/library_check.py:298-370`、`:500-533`
- Test: `backend/tests/test_library_check_timeout.py`（新建）

**Interfaces:**
- Consumes: `_mark_timeout_if_expired(dq_id, media_id, episode, quark_path, started_at, timeout_seconds, now)`（现有函数，复用）
- Produces: 遗漏集命中 & tmdb_id 缺失两条 continue 路径纳入超时；各 continue 分支日志含明确原因

- [ ] **Step 1: 写失败测试**（新建 `backend/tests/test_library_check_timeout.py`）

```python
"""入库确认卡死修复：遗漏集/缺 tmdb_id 路径纳入超时窗口。"""
# 用例：
# 1. Emby 命中、_episode_in_missing 恒 True、超过 timeout → 任务置 failed（node_error 含超时原因），不再无限等待
# 2. Emby 命中、遗漏集命中但未超时 → 保持 library 等待（不误杀）
# 3. media.tmdb_id None 且超时 → 置 failed 并记录「缺 tmdb_id」原因
# 4. find_emby_id 抛异常（Emby 故障）→ 仍本轮跳过不消耗超时（保持既有语义）
# 5. _finalize_done 正常路径不回归（Emby 收录 → done）
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && pytest tests/test_library_check_timeout.py -v`
Expected: FAIL（遗漏集路径当前不超时，断言超时置 failed 失败）

- [ ] **Step 3: 修复实现**

`library_check.py` 主循环（342-370）重构：

```python
        if emby_id:
            # P1-2 集级入库确认（保持原语义：追更新集刚刮削完仍是遗漏集时不误删夸克）
            if (media.media_type or "").strip().lower() != "movie":
                try:
                    missing = await emby.get_missing_episodes(emby_id)
                except Exception as exc:  # noqa: BLE001  含 EmbyUnavailable（Emby 故障）
                    logger.warning(
                        "[library_check] media=%s Emby 遗漏集查询失败（本轮跳过，不误判，不消耗超时）: %s",
                        media_id, exc,
                    )
                    continue
                missing_codes: set[str] = {
                    str(ep.get("code")) for ep in missing if ep.get("code")
                }
                if _episode_in_missing(episode, missing_codes):
                    # 卡死修复：遗漏集路径同样受超时窗口约束 —— 追更新集长期在遗漏集
                    # （Emby 刮削一直不收录）不再无限等待，超时走 failed 并记录原因。
                    await _mark_timeout_if_expired(
                        dq_id, media_id, episode, quark_path, started_at,
                        timeout_seconds, now,
                        cause="持续在 Emby 遗漏集（Emby 收录超时）",
                    )
                    continue
            await _finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod)
            continue
```

`_mark_timeout_if_expired`（500-533）签名加 `cause: str = "Emby 未收录"`：

```python
async def _mark_timeout_if_expired(dq_id, media_id, episode, quark_path, started_at,
                                   timeout_seconds, now, cause: str = "Emby 未收录") -> None:
    """入库 / 遗漏集 / 配置缺失统一超时判定；node_started_at 缺失（旧数据）保守不判定。"""
    if started_at is None:
        return
    if started_at + timedelta(seconds=timeout_seconds) >= now:
        return  # 未超时，下一轮再查
    err = f"入库超时：{cause}，请人工核实刮削/收录配置"
    ...
```

主循环 tmdb_id 缺失分支（333-335）改为：

```python
        if media.tmdb_id is None:
            await _mark_timeout_if_expired(
                dq_id, media_id, episode, quark_path, started_at,
                timeout_seconds, now,
                cause="media 缺 tmdb_id（配置缺失）",
            )
            continue
```

其余 continue 分支日志补 media_id/episode/原因（保持「本轮跳过不消耗超时」语义，但日志明确）：
- `find_emby_id` 异常分支（338-340）：`"[library_check] media=%s %s Emby 收录查询失败（本轮跳过，不消耗超时）: %s"`

- [ ] **Step 4: 运行通过 + 全量回归**

Run: `cd backend && pytest tests/test_library_check_timeout.py tests/test_council_fixes.py -v`
Expected: PASS，无回归

- [ ] **Step 5: 勾选并提交**

```
勾选 tasks.md 3.1 / 3.2 / 3.3
git add backend/app/tasks/library_check.py backend/tests/test_library_check_timeout.py docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "fix(queue)：入库确认卡死修复——遗漏集/缺 tmdb_id 路径纳入超时窗口
- 根因：Emby 命中但遗漏集持续命中时 continue 不经过超时判定，永不 finalize
- _mark_timeout_if_expired 支持自定义原因，遗漏集/配置缺失超时置 failed 并记录
- 各 continue 分支日志补明确原因（Emby 故障仍不消耗超时，避免误杀）"
```

---

### Task 4: 前端 巡检队列改名、字段、分页、分享码链接（QueueView + store + api + types）

**Files:**
- Modify: `frontend/src/views/QueueView.vue`、`frontend/src/stores/queue.ts`、`frontend/src/api/index.ts:96-123`、`frontend/src/types/index.ts:130-212`、`frontend/src/utils/format.ts`
- Test: `frontend/src/views/QueueView.test.ts`（新建）、`frontend/src/utils/format.test.ts`（追加）

**Interfaces:**
- Consumes: 后端 `{items,total}` 契约；`QueueTaskItem.share_code/size_estimated`；`DownloadQueueItem.share_url/size_estimated`
- Produces: `store.fetchPage/fetchDownloadPage` 读 total 分页；`store.total/downloadTotal` 状态；`format.ts` 新增 `formatFileSize(size, estimated)`（约前缀）

- [ ] **Step 1: types 与 api 契约同步**（`frontend/src/types/index.ts` / `frontend/src/api/index.ts`）

`QueueTaskItem`（132-148）补：

```typescript
  /** 明文分享码（仅 admin 返回；guest 为 null） */
  share_code?: string | null
  /** file_size 是否为均摊估算值（展示「约」前缀） */
  size_estimated?: boolean
```

`DownloadQueueItem`（190-212）补：

```typescript
  /** 夸克分享地址（admin 可见；无分享码为 null） */
  share_url?: string | null
  /** file_size 是否为均摊估算值 */
  size_estimated?: boolean
```

`listQueueApi`/`listDownloadQueueApi`（103-123）返回类型改 `{items, total}`：

```typescript
export interface QueueListResponse<T> {
  items: T[]
  total: number
}

export function listQueueApi(limit = 50, offset = 0) {
  return http
    .get<QueueListResponse<QueueTaskItem>>('/queue', { params: { limit, offset } })
    .then((r) => r.data)
}

export function listDownloadQueueApi(limit = 50, offset = 0) {
  return http
    .get<QueueListResponse<DownloadQueueItem>>('/queue', { params: { type: 'download', limit, offset } })
    .then((r) => r.data)
}
```

- [ ] **Step 2: store 分页重写**（`frontend/src/stores/queue.ts`）

state 调整：`page: 1`、`pageSize: 20`、`total: 0`、`downloadPage: 1`、`downloadTotal: 0`；删除 `hasMore`/`downloadHasMore`。

```typescript
    async fetchPage(goPage = this.page): Promise<void> {
      this.loading = true
      try {
        const res = await listQueueApi(this.pageSize, (goPage - 1) * this.pageSize)
        this.items = res.items
        this.total = res.total
        this.page = goPage
      } finally {
        this.loading = false
      }
    },
```

```typescript
    async fetchDownloadPage(goPage = this.downloadPage): Promise<void> {
      this.downloadLoading = true
      try {
        const res = await listDownloadQueueApi(this.pageSize, (goPage - 1) * this.pageSize)
        this.downloadItems = res.items
        this.downloadTotal = res.total
        this.downloadPage = goPage
      } catch {
        // 失败保留旧数据，拦截器已提示
      } finally {
        this.downloadLoading = false
      }
    },
```

（`pageSize` 保持 20 或改 50 与后端默认对齐——取 50，减少翻页频次；两种 Tab 各自维护页码，切 Tab 回第 1 页由调用方 `goPage=1`。）

- [ ] **Step 3: format.ts 约标注 helper**

```typescript
/** 文件大小展示：估算值加「约 」前缀，无值显示 — */
export function formatFileSize(fileSize: number | null | undefined, estimated?: boolean): string {
  if (fileSize == null) return '—'
  const s = formatBytes(fileSize)
  return estimated ? `约 ${s}` : s
}
```

- [ ] **Step 4: QueueView.vue 改造**

a) **Tab 改名**：`:306` `<h3>任务队列</h3>` → `巡检队列`；`:337` `label="任务队列"` → `label="巡检队列"`（`name="task"` 不变）。

b) **贡献码/大小列**（任务 Tab 356-390 区）：
- `大小` 列改为 `{{ formatFileSize(row.file_size, row.size_estimated) }}`（导入 formatFileSize 代替 formatBytes 于该列）
- 新增「分享码」列（admin 才显示）：`<el-table-column prop="share_code" label="分享码" width="140"><template #default="{ row }">{{ row.share_code || '—' }}</template></el-table-column>`；非 admin（`auth.user?.role !== 'admin'`）用 `v-if` 隐藏该列（参照 auth store 现有 admin 判定用法）

c) **下载 Tab 分享码明文链接**（610-617 区）：

```vue
<el-table-column prop="share_code" label="分享码" width="150">
  <template #default="{ row }">
    <a
      v-if="row.share_url && row.share_code"
      :href="row.share_url"
      target="_blank"
      rel="noopener"
      class="qv-share-link"
    >{{ row.share_code }}</a>
    <span v-else-if="row.share_code">{{ row.share_code }}</span>
    <span v-else>—</span>
  </template>
</el-table-column>
```

样式 `.qv-share-link`（727-736 样式区追加）：蓝色 `color: var(--el-color-primary); text-decoration: underline; word-break: break-all;`

d) **el-pagination 替代 loadMore**：删除 `loadMore()`（266-269）与两个「加载更多」按钮块（477-479 / 687-689）；两个 Tab 表格下方/上方各加：

```vue
<el-pagination
  v-if="store.total > store.pageSize"
  :current-page="store.page"
  :page-size="store.pageSize"
  :total="store.total"
  layout="prev, pager, next"
  @current-change="(p: number) => store.fetchPage(p)"
/>
```

下载 Tab 同理用 `store.downloadPage/downloadTotal` + `store.fetchDownloadPage(p)`。切 Tab 时（`onTabChange` 235-242）下载首拉 `fetchDownloadPage(1)`。

e) **慢刷调整**（247-252）：15s 慢刷保留，但只刷当前页（`store.fetchPage(store.page)` 保持当前页不跳回第 1 页）。

- [ ] **Step 5: 前端测试**（新建 `frontend/src/views/QueueView.test.ts`，参照 `MediaDetailView.test.ts` 的 vitest+mount 基建）

```typescript
// 用例：
// 1. 任务 Tab label 渲染「巡检队列」
// 2. 巡检队列行展示 name/SxxExx/大小（size_estimated=true 显示「约 」前缀）
// 3. share_url 存在 → 渲染 <a href> 可点击；无 → 纯文本不可点击
// 4. el-pagination 依 total 渲染 / 翻页回调调用 fetchPage(page)
// 5. 分享码列 admin 可见 / guest 隐藏
```

`format.test.ts` 追加：`formatFileSize(null,...)='—'`、`formatFileSize(1e9,false)='1.00 GB'`、`formatFileSize(1e9,true)='约 1.00 GB'`。

Run: `cd frontend && npm run test`
Expected: 新用例 PASS，全量 vitest 通过

- [ ] **Step 6: 勾选并提交**

```
勾选 tasks.md 4.1 / 4.2 / 4.3 / 4.4 / 4.5 / 4.6
git add frontend/src/views/QueueView.vue frontend/src/stores/queue.ts frontend/src/api/index.ts frontend/src/types/index.ts frontend/src/utils/format.ts frontend/src/views/QueueView.test.ts frontend/src/utils/format.test.ts docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "feat(queue)：巡检队列改名、字段补全、el-pagination 分页、分享码明文链接
- 「任务队列」Tab 更名「巡检队列」，行补分享码列（admin）
- 大小估算值约标注（formatFileSize），下载队列分享码明文可点击跳夸克
- loadMore 移除改 el-pagination 标准分页（page/pageSize/total），慢刷保持当前页
- types/api/store 契约同步 {items,total}，补 QueueView vitest"
```

---

### Task 5: 验证与收尾（tasks 5.1）

**Files:**
- Test: 前端构建 `npm run build`；后端启动/测试全量

- [ ] **Step 1: 全量测试**

Run: `cd backend && pytest` 与 `cd frontend && npm run test`
Expected: 全部通过

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 构建成功无报错

- [ ] **Step 3: 后端迁移冒烟**

Run: `cd backend && alembic upgrade head`（或说明依赖服务在启动期自动执行 init_db；不主动启服务，若需验证请用户在可验证环境执行）
Expected: 迁移 0016 成功到 head

- [ ] **Step 4: 勾选并提交**

```
勾选 tasks.md 5.1
git add docs/openspec/changes/queue-inspection-rework/tasks.md
git commit -m "chore(queue)：验证完成——pytest/vitest 全过、前端构建通过、迁移 0016 到 head"
```

---

## Self-Review

**Spec 覆盖：**
- 巡检队列改名 → Task 4a ✓
- 巡检队列字段（影视名称/SxxExx/分享码/状态/大小/更新时间）→ 现有 title+episode+status+新增 share_code+file_size+updated_at，Task 1/4 ✓
- 队列大小真实值（逐行真实、缺失 —、估算约标注、下载后回填）→ Task 1（size_estimated 输出）+ Task 2（回填）+ Task 4（formatFileSize）✓
- 分页 + 创建时间升序 → Task 1（排序+total）+ Task 4（el-pagination）✓
- 下载队列分享码明文可点击、无分享地址降级 → Task 1（share_url）+ Task 4c ✓
- 列表默认显示全部 / 去展开更多 → Task 4d（el-pagination 替代 loadMore）✓
- 入库确认正常完成 / 失败归因 / 遗漏集不无限等待 → Task 3 ✓
- 分享码权限边界（admin 明文 / guest 不返回）→ Task 1 脱敏 + Task 4b ✓

**占位符扫描：** 测试骨架中 `...` 为待接线 fixture 的真实断言实现，已注明参照现有测试文件写法（test_media_two_queue.py），Task 内可安全补全。无 TBD/TODO。

**类型一致性：** `_list_flat`/`_list_download` 均返回 `{items,total}`；前端 `QueueListResponse<T>` 与 store `fetchPage/fetchDownloadPage` 对齐；`size_estimated` 贯穿后端模型→路由→前端类型→formatFileSize。