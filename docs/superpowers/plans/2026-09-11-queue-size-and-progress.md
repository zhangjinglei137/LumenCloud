---
change: queue-size-and-progress
design-doc: docs/superpowers/specs/2026-09-11-queue-size-and-progress-design.md
base-ref: 273a9d3ba49b04693810355907be5e1ecfc537de
---

# queue-size-and-progress 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 按 build_mode 选择 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务实施。步骤使用 checkbox（`- [ ]`）跟踪。

**Goal:** 队列大小精确值优先展示（下载中行实时精确、估算仅「约」兜底、0/空值显示「—」），下载队列进度与大小分列展示。

**Architecture:** 探测阶段因 cloudSaver 无单文件 size 保持均摊估算兜底（外部 issue #130 跟踪）；精确大小主来源是 aria2 `totalLength`——`/queue/download/progress` 响应新增 `total` 字段供下载中行实时展示，下载完成持久化回填保持现状（transfer.py 已实现）。前端 `formatFileSize` 补 ≤0 语义，下载队列「进度/大小」混列拆为两列。

**Tech Stack:** FastAPI + SQLAlchemy（backend）、Vue 3 + Element Plus + Pinia（frontend）、pytest / vitest。

**Spec:** `docs/superpowers/specs/2026-09-11-queue-size-and-progress-design.md`（设计决策 D1-D4）、`docs/openspec/changes/queue-size-and-progress/specs/queue-inspection-display/spec.md`（delta spec）、`docs/openspec/changes/queue-size-and-progress/tasks.md`（任务边界）。

## Global Constraints

- 产物语言 zh-CN：commit message 用中文、Conventional Commits 结构（`feat|fix|test(scope): 中文摘要`）。
- **无新增依赖、无数据库变更、无迁移**（Design Doc §7）。
- 后端测试运行：`cd backend && ./.venv/bin/python -m pytest <path> -v`（系统 python 无依赖，必须用 backend/.venv）。
- 前端测试运行：`cd frontend && npm run test -- <file>`（vitest）或 `npx vitest run <file>`。
- 前端构建：`cd frontend && npm run build`。
- 只覆盖 tasks.md 列出的 8 个任务，不扩展范围。

## 现状事实（执行者必读，勿重复调研）

- `backend/app/routers/queue.py:831` `download_progress` 返回 `{id, gid, speed, progress, download_name}`，**无 total**；`tell_status` 成功时 `total=int(st.get("totalLength") or 0)`、`done=int(st.get("completedLength") or 0)`。
- `backend/tests/test_queue.py:661` `test_progress_aggregates_tell_status_and_degrades`：现有 progress 聚合测试（fake tell_status：gid-1 返回 totalLength="1000"/completedLength="250"/downloadSpeed="500"；gid-2 抛 RuntimeError）。断言行 680-684。
- **已有测试覆盖**（勿重复写）：`backend/tests/test_queue_size_estimated.py`（size_estimated 写入/取件拷贝/`_complete_download` 回填清标记）、`backend/tests/test_scan_walk_size.py`（walk_share 单文件 size 读取与均摊估算）、`frontend/src/views/QueueView.test.ts:174`（巡检队列「约 」标注渲染）。
- `frontend/src/utils/format.ts:26` `formatFileSize(fileSize, estimated?)`：当前 `fileSize == null → '—'`，否则 `estimated ? '约 X' : X`。**未处理 ≤0**（0 会走 `formatBytes(0)` → "0 B"）。
- `frontend/src/views/QueueView.vue:602-616`「进度 / 大小」列：`downloading` 渲染进度条+速度（无大小）；非 downloading 渲染 `formatBytes(row.file_size)`（无「约」）。
- `frontend/src/types/index.ts:230` `DownloadProgressEntry {id?, gid?, speed?, progress?}`。
- 巡检队列大小列（`QueueView.vue:376-380`）已是 `formatFileSize(row.file_size, row.size_estimated)`，**不改模板**，仅补测试。

---

### Task 1: 后端 progress 接口新增 total 字段（tasks 1.1 核查 + 1.2 落地）

**Files:**
- Modify: `backend/app/routers/queue.py:858-880`（`download_progress` 循环体）
- Test: `backend/tests/test_queue.py:661-684`（扩展 `test_progress_aggregates_tell_status_and_degrades`）

**Interfaces:**
- Consumes: `aria2.client.tell_status(gid)` 返回 `{totalLength, completedLength, downloadSpeed, ...}`（字符串数字）。
- Produces: `download_progress` 响应每个 entry 新增 `"total": int | None`（totalLength>0 时字节数，否则 None）。前端类型 `DownloadProgressEntry.total` 在 Task 3 扩展。

**核查结论（tasks 1.1）**：真实大小来源为 aria2 `totalLength`（下载链路）+ 分享总大小均摊（探测链路，cloudSaver 无单文件 size，外部 issue jiangrui1994/CloudSaver#130）；`size_estimated` 判定在 scan.py `_walk_share`（缺失均摊置 True）/ `_complete_download`（回填清 False）。写入/拷贝/回填/估算链路测试均已存在（见现状事实），无需新增。

- [ ] **Step 1: 扩展测试（Red）**——在 `test_progress_aggregates_tell_status_and_degrades` 的断言区（680-684 行后）追加：

```python
    # total：totalLength>0 → 真实字节；失败行降级 None
    assert by_gid["gid-1"]["total"] == 1000
    assert by_gid["gid-2"]["total"] is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_queue.py::test_progress_aggregates_tell_status_and_degrades -v`
Expected: FAIL——`KeyError: 'total'`（接口未返回 total 字段）。

- [ ] **Step 3: 实现**——`queue.py` `download_progress` 循环内（859-880 行），在 `speed` 处理之后追加 total：

```python
            if total > 0:
                entry["progress"] = round(done / total * 100, 1)
                entry["total"] = total
```

`entry` 初始 dict（862-868 行）中补 `"total": None`，保持失败行降级语义：

```python
        entry: dict = {
            "id": dq.id,
            "gid": dq.aria2_gid,
            "speed": None,
            "progress": None,
            "total": None,
            "download_name": dq.download_name,
        }
```

注意：`totalLength` 缺失/0/非法时保持 `None`（不显示 0 字节，Design Doc §4 边界条件）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_queue.py::test_progress_aggregates_tell_status_and_degrades -v`
Expected: PASS（total=1000 与 total=None 两条新断言 + 原断言）。

- [ ] **Step 5: 回归整个队列测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_queue.py tests/test_queue_size_estimated.py -v`
Expected: 全部 PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/routers/queue.py backend/tests/test_queue.py
git commit -m "feat(queue): progress 接口返回 aria2 真实总大小 total
下载中行可实时展示精确大小（totalLength>0 时返回，缺失/0 降级 None）"
```

- [ ] **Step 7: 勾选 tasks.md 1.1 与 1.2**

tasks.md 中 `- [ ] 1.1 ...` 与 `- [ ] 1.2 ...` 改为 `- [x]`。

---

### Task 2: 后端测试固化确认（tasks 1.3）

**Files:**
- 无源码改动（只验证 + 视缺口补断言）
- Test: `backend/tests/test_queue_size_estimated.py`、`backend/tests/test_scan_walk_size.py`

**Interfaces:**
- Consumes: 现状事实中列出的既有测试文件。
- Produces: 无新接口；确认后端链路测试完整。

- [ ] **Step 1: 运行既有链路测试**

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_queue_size_estimated.py tests/test_scan_walk_size.py -v`
Expected: 全部 PASS（覆盖：size_estimated 写入/拷贝/回填清标记、walk_share 单文件 size 读取与均摊估算）。

- [ ] **Step 2: 核查列表接口透传断言**——`backend/tests/test_queue.py` 中 `test_list_download_flat_type_download`（272 行）与 `test_list_flat_contract_fields_and_no_credentials`（242 行）应含 `size_estimated` 断言；若缺失，在该测试的响应行断言区追加：

```python
    assert rows[0]["size_estimated"] is False
```

Run: `cd backend && ./.venv/bin/python -m pytest tests/test_queue.py -k "list_" -v`
Expected: 全部 PASS。

- [ ] **Step 3: 提交（如有断言补充）**

```bash
git add backend/tests/test_queue.py
git commit -m "test(queue): 固化列表接口 size_estimated 透传断言"
```

（无断言补充时跳过本步，直接进入 Step 4。）

- [ ] **Step 4: 勾选 tasks.md 1.3**

tasks.md 中 `- [ ] 1.3 ...` 改为 `- [x]`。

---

### Task 3: 前端 formatFileSize ≤0 语义 + 类型扩展（tasks 2.1）

**Files:**
- Modify: `frontend/src/utils/format.ts:26-30`（`formatFileSize`）
- Modify: `frontend/src/types/index.ts:230-239`（`DownloadProgressEntry` 加 `total`）
- Test: `frontend/src/utils/format.test.ts:104-120`（扩展 formatFileSize describe）

**Interfaces:**
- Consumes: `formatBytes(bytes)`（format.ts:16，`null/NaN/<0 → '—'`，`>=1GB → "X.XX GB"`）。
- Produces: `formatFileSize(fileSize: number | null | undefined, estimated?: boolean): string`——`fileSize == null || fileSize <= 0 → '—'`；否则 `estimated ? '约 X' : X`。`DownloadProgressEntry.total?: number | null`。

- [ ] **Step 1: 写失败测试**——在 `format.test.ts` 的 `describe('文件大小约标注...')`（104-120 行）内追加：

```typescript
  it('0 与负数视为无记录 → —（不显示 0 B）', () => {
    expect(formatFileSize(0)).toBe('—')
    expect(formatFileSize(0, true)).toBe('—')
    expect(formatFileSize(-5)).toBe('—')
  })
```

同时扩展 `DownloadProgressEntry` 类型（types/index.ts:230-239）：

```typescript
export interface DownloadProgressEntry {
  /** download_queue.id（或 gid 二选一，后端契约以 id 为准） */
  id?: number | null
  gid?: string | null
  /** 字节/秒 */
  speed?: number | null
  /** 0~100 */
  progress?: number | null
  /** aria2 totalLength 真实总大小（字节）；缺失/0 为 null */
  total?: number | null
  [key: string]: unknown
}
```

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: FAIL——`formatFileSize(0)` 返回 `"0 B"`（当前 `formatBytes(0)` 分支）而非 `"—"`。

- [ ] **Step 3: 实现**——format.ts `formatFileSize`（26-30 行）改为：

```typescript
/** 文件大小展示：估算值加「约 」前缀；无值或 ≤0 显示 —（0 字节影视场景不存在） */
export function formatFileSize(fileSize: number | null | undefined, estimated?: boolean): string {
  if (fileSize == null || fileSize <= 0) return '—'
  const s = formatBytes(fileSize)
  return estimated ? `约 ${s}` : s
}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: PASS（新增 0/负数 断言 + 既有四态断言）。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/utils/format.test.ts frontend/src/types/index.ts
git commit -m "feat(ui): formatFileSize 零值与负值显示 —，progress 类型补 total
0 字节影视场景不存在，杜绝「0 B」误导；total 供下载中行实时精确大小"
```

- [ ] **Step 6: 勾选 tasks.md 2.1**

tasks.md 中 `- [ ] 2.1 ...` 改为 `- [x]`。

---

### Task 4: 巡检队列大小列验证补测（tasks 2.2）

**Files:**
- Modify: `frontend/src/views/QueueView.test.ts`（巡检队列 describe 内追加断言）
- 不改 `QueueView.vue` 巡检队列模板（376-380 行已条件化，Design Doc D3）

**Interfaces:**
- Consumes: `storeState.items`（test 文件 9-41 行 mock）；`makeTask()`（53-67 行 helper）；EP_STUBS + `mountView()`（92-141 行，el-table 通过 TABLE_ROWS provide 渲染 scoped slot）。
- Produces: 无新接口；验证巡检队列大小列 `formatFileSize` 条件语义。

- [ ] **Step 1: 写测试**——在 `describe('QueueView 巡检队列 Tab'...)`（164 行起）内追加：

```typescript
  it('巡检队列大小列：file_size=0 显示「—」，精确值无「约」', async () => {
    storeState.items = [makeTask({ file_size: 0, size_estimated: false })]
    wrapper = mountView()
    await flushPromises()
    // 大小单元格只渲染一次且为「—」（0 值语义，Task 3 的 formatFileSize）
    const text = wrapper.text()
    expect(text).toContain('—')
    expect(text).not.toContain('0 B')
    expect(text).not.toContain('约')
  })
```

（参考既有 174 行测试「巡检队列行渲染标题、集号与『约 』标注大小」的断言风格，它已覆盖 `size_estimated=true` 时「约 」标注渲染。）

- [ ] **Step 2: 运行确认通过**

Run: `cd frontend && npx vitest run src/views/QueueView.test.ts`
Expected: PASS（新增断言 + 既有 174 行「约 」断言）。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/views/QueueView.test.ts
git commit -m "test(ui): 巡检队列大小列 0 值显示 — 与精确值无约前缀断言"
```

- [ ] **Step 4: 勾选 tasks.md 2.2**

tasks.md 中 `- [ ] 2.2 ...` 改为 `- [x]`。

---

### Task 5: 下载队列拆列：进度与大小独立 + total 优先（tasks 2.3 + 2.4）

**Files:**
- Modify: `frontend/src/views/QueueView.vue:602-616`（「进度 / 大小」列 → 两列）
- Test: `frontend/src/views/QueueView.test.ts`（追加下载队列拆列 describe）

**Interfaces:**
- Consumes: `downloadProgress(d)`（QueueView.vue:149，`store.progressMap[id].progress`）、`downloadSpeed(d)`（155 行）、`store.progressMap[id].total`（新，类型见 Task 3）、`formatFileSize` / `formatBytes`（format.ts）。
- Produces: 模板两列——「进度」列（仅 `downloading` 显示进度条+速度，其余「—」）；「大小」列（`downloading` 行 `progressMap[id].total ?? file_size` 经 `formatFileSize` 展示，其余行 `formatFileSize(file_size, size_estimated)`）。

- [ ] **Step 1: 写失败测试**——在 `QueueView.test.ts` 末尾追加：

```typescript
describe('QueueView 下载队列拆列（queue-size-and-progress）', () => {
  it('下载中行：进度列含进度条+速度，大小列优先 progressMap.total 精确值', async () => {
    storeState.downloadItems = [makeDownload({ id: 7, status: 'downloading', file_size: 1024 ** 3, size_estimated: true })]
    storeState.progressMap = { 7: { id: 7, progress: 50, speed: 512 * 1024, total: 2 * 1024 ** 3 } }
    wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    // 大小列展示 total 精确值（2.00 GB），不出现「约」与 1GB 估算值
    expect(text).toContain('2.00 GB')
    expect(text).not.toContain('约')
    expect(text).not.toContain('1.00 GB')
  })

  it('下载中行 total 缺失：大小列回退 file_size 并带「约」（估算兜底）', async () => {
    storeState.downloadItems = [makeDownload({ id: 8, status: 'downloading', file_size: 1024 ** 3, size_estimated: true })]
    storeState.progressMap = { 8: { id: 8, progress: 30, speed: null, total: null } }
    wrapper = mountView()
    await flushPromises()
    expect(wrapper.text()).toContain('约 1.00 GB')
  })

  it('非下载中行：无进度条，仅大小列（精确值）', async () => {
    storeState.downloadItems = [makeDownload({ id: 9, status: 'pending', file_size: 2 * 1024 ** 3, size_estimated: false })]
    wrapper = mountView()
    await flushPromises()
    const text = wrapper.text()
    expect(text).toContain('2.00 GB')
    expect(text).not.toContain('约')
  })
})
```

注意：`makeDownload`（69-85 行）默认 `file_size: null`，本测试显式传值；`progressMap` 需在 beforeEach 重置（156 行后追加 `storeState.progressMap = {}`）。

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && npx vitest run src/views/QueueView.test.ts`
Expected: FAIL——`"2.00 GB"` 不出现（下载中行当前不渲染大小）且 `"约 1.00 GB"` 不出现（非下载中行用 `formatBytes` 无「约」）。

- [ ] **Step 3: 实现**——QueueView.vue 模板 602-616 行替换为两列：

```vue
              <el-table-column label="进度" width="200">
                <template #default="{ row }">
                  <!-- downloading：2.5s 局部轮询的实时进度条 + 速度 -->
                  <div v-if="row.status === 'downloading'">
                    <template v-if="downloadProgress(row) != null">
                      <el-progress :percentage="downloadProgress(row)!" :stroke-width="8" />
                      <span v-if="downloadSpeed(row)" class="lc-muted" style="font-size: 12px">
                        {{ downloadSpeed(row) }}
                      </span>
                    </template>
                    <el-progress v-else :percentage="100" :stroke-width="8" striped striped-flow :show-text="false" status="warning" />
                  </div>
                  <span v-else class="lc-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="大小" width="140">
                <template #default="{ row }">
                  <!-- 下载中行优先 aria2 total 精确值（实时）；缺失回退 file_size（估算带「约」） -->
                  <!-- Ruling（协调者 2026-09-11）：estimated 参数条件化——total 存在时为精确值强制无「约」，
                       否则用 row.size_estimated（total 是 aria2 真实值，不应带「约」；与测试 1 断言一致） -->
                  <span style="font-size: 13px">
                    {{ formatFileSize(row.status === 'downloading' ? (store.progressMap[row.id]?.total ?? row.file_size) : row.file_size, row.status === 'downloading' && store.progressMap[row.id]?.total != null ? false : row.size_estimated) }}
                  </span>
                </template>
              </el-table-column>
```

同时补齐 script 中 `progressMap` 已由 store 提供（QueueView.vue 未直接引用过 total，类型已在 Task 3 扩展）。

- [ ] **Step 4: 运行确认通过**

Run: `cd frontend && npx vitest run src/views/QueueView.test.ts`
Expected: PASS（新增 3 断言 + 既有全部断言）。

- [ ] **Step 5: 前端类型检查 + 全量前端测试**

Run: `cd frontend && npx vue-tsc --noEmit && npx vitest run`
Expected: 类型零错误、全部测试 PASS。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/views/QueueView.vue frontend/src/views/QueueView.test.ts
git commit -m "feat(ui): 下载队列拆列，进度与大小分列且下载中行优先真实 total
进度列仅下载中渲染进度条+速度；大小列全行展示，total 缺失回退估算带「约」"
```

- [ ] **Step 7: 勾选 tasks.md 2.3 与 2.4**

tasks.md 中 `- [ ] 2.3 ...` 与 `- [ ] 2.4 ...` 改为 `- [x]`。

---

### Task 6: 集成验证（tasks 3.1）

**Files:**
- 无源码改动（验证任务）。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && ./.venv/bin/python -m pytest -v`
Expected: 全部 PASS。

- [ ] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: 零报错（构建产物生成）。

- [ ] **Step 3: 前端全量测试（含新增拆列/格式化断言）**

Run: `cd frontend && npx vitest run`
Expected: 全部 PASS。

- [ ] **Step 4: 记录构建证据**

```bash
comet state record-check queue-size-and-progress build --command "cd backend && ./.venv/bin/python -m pytest -v && cd frontend && npm run build && npx vitest run" --exit-code 0
```

- [ ] **Step 5: 起服目视指引（需用户操作）**

告知用户本地起服查看巡检/下载队列展示（下载中行大小精确、进度独立列、「约」仅估算兜底、0/空显示「—」），并说明服务启动由用户自行执行（全局规则）。

- [ ] **Step 6: 勾选 tasks.md 3.1**

tasks.md 中 `- [ ] 3.1 ...` 改为 `- [x]`。

---

## Self-Review

**Spec 覆盖核对：**
- delta spec「队列大小真实值」场景：精确优先 → Task 1（aria2 total）+ Task 5（total 优先）；估算标注「约」→ Task 3/5（formatFileSize estimated）；大小缺失「—」→ Task 3（≤0 语义）+ Task 4（0 值断言）；下载后回填 → 现状已实现（test_queue_size_estimated.py 覆盖，Task 2 确认）。
- delta spec「下载队列展示进度与大小」场景：下载中分行展示 → Task 5；下载中行精确大小 → Task 1 + Task 5；非下载中状态 → Task 5。
- design.md D1（估算兜底）/ D2（progress total）/ D3（拆列）/ D4（formatFileSize）→ Task 1/3/5 逐条落地。
- tasks.md 8 个任务全部映射（T1: 1.1+1.2、T2: 1.3、T3: 2.1、T4: 2.2、T5: 2.3+2.4、T6: 3.1）。

**Placeholder 检查：** 无 TBD/TODO；所有代码步骤含实际内容；类型/函数名与 Design Doc §3 一致（`DownloadProgressEntry.total`、`formatFileSize` 四态、`progressMap[id].total`）。

**类型一致性：** Task 3 定义 `DownloadProgressEntry.total` → Task 5 消费 `store.progressMap[row.id]?.total`；`formatFileSize` 签名（Task 3 调整）→ Task 4/5 调用一致。
