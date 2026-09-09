# docker-timezone 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Docker 容器时区固定东八区、后端时间字段统一 UTC+Z 标注、前端显式按东八区渲染。

**Architecture:** 三层横切改动——(1) compose 文件给 lumencloud/db 注入 `TZ: Asia/Shanghai`；(2) 后端在 FastAPI `jsonable_encoder` 边界做全局 UTC+Z 归一（naive 追加 `Z`，aware 转 UTC 补 `Z`），存储保持 UTC naive 不变；(3) 前端 `format.ts` 统一解析（无时区后缀按 UTC）+ `Intl.DateTimeFormat` 东八区渲染，所有视图经统一入口生效。

**Tech Stack:** docker-compose、FastAPI（Python）、TypeScript/Vue3、Vitest。

**Spec:** `docs/superpowers/specs/2026-09-09-docker-timezone-design.md`（Design Doc）；`docs/openspec/changes/docker-timezone/specs/timezone-display/spec.md`（delta spec）

## Global Constraints

- DB 存储保持 UTC naive，不迁移、不重算（`now_utc_naive()`、`server_default=CURRENT_TIMESTAMP` 不动）
- APScheduler 调度语义不动
- 后端**不逐字段手改**序列化点，只在编码边界统一归一
- 前端不逐视图改动：仅改 `format.ts` 统一入口；`isFutureDate`（日期粒度）不改
- 产物语言：zh-CN（代码注释、commit message）
- 提交信息遵循 Conventional Commits，描述用中文，摘要 ≤50 字符动词开头

---

### Task 1: compose 注入 TZ 环境变量

**Files:**
- Modify: `docker-compose.yml:34-38`（db environment）、`docker-compose.yml:69-72`（lumencloud environment）
- Modify: `docker-compose.prod.yml`（db 与 lumencloud 的 environment 块，行号以实际为准）

**Interfaces:**
- Consumes: 无
- Produces: 两个 compose 文件的 lumencloud 与 db 服务 `environment` 均含 `TZ: Asia/Shanghai`

- [x] **Step 1: 编辑 `docker-compose.yml`**

db 服务 environment 块：

```yaml
    environment:
      POSTGRES_USER: lumen
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-lumencloud}
      POSTGRES_DB: lumencloud
      TZ: Asia/Shanghai
```

lumencloud 服务 environment 块：

```yaml
    environment:
      DATABASE_URL: postgresql+asyncpg://lumen:${POSTGRES_PASSWORD:-lumencloud}@db:5432/lumencloud
      TZ: Asia/Shanghai
```

- [x] **Step 2: 编辑 `docker-compose.prod.yml`**

先读取文件确认 db 与 lumencloud 的 environment 块位置，在两者各追加 `TZ: Asia/Shanghai`（保持原有键不变）。

- [x] **Step 3: 验证 compose 解析**

Run: `docker compose config --quiet`（项目根）
Expected: 退出码 0，无输出（或仅提示无警告）；无 YAML 语法错误

- [x] **Step 4: 提交**

```bash
git add docker-compose.yml docker-compose.prod.yml
git commit -m "feat(docker): 为 lumencloud/db 容器注入 TZ=Asia/Shanghai"
```

---

### Task 2: 后端全局 UTC+Z 序列化

**Files:**
- Create: `backend/app/json.py`
- Modify: `backend/app/main.py:100`（FastAPI 实例化前接线）
- Modify: `backend/app/routers/queue.py:71-72`（`_iso` 补 Z）
- Test: `backend/tests/test_json_encoder.py`

**Interfaces:**
- Consumes: FastAPI `jsonable_encoder` 原函数（`fastapi.encoders`）
- Produces: `backend/app/json.py` 导出 `install_zulu_encoder()`（接线函数，幂等）；所有 API 响应 datetime 均带 `Z` 后缀

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_json_encoder.py`：

```python
"""docker-timezone：全局 UTC+Z 编码器单测。"""
from datetime import datetime, timezone

import pytest
from fastapi.encoders import jsonable_encoder

from app.json import jsonable_encoder_zulu


def test_naive_datetime_appends_z():
    dt = datetime(2026, 9, 9, 1, 15, 0)  # naive，按 UTC 解释
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_aware_utc_normalized_to_z():
    dt = datetime(2026, 9, 9, 1, 15, 0, tzinfo=timezone.utc)
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_aware_plus0800_normalized_to_z():
    tz = timezone.__new__(timezone, __import__("datetime").timedelta(hours=8))
    dt = datetime(2026, 9, 9, 9, 15, 0, tzinfo=tz)
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_non_datetime_delegates_to_original():
    assert jsonable_encoder_zulu("hello") == "hello"
    assert jsonable_encoder_zulu({"a": 1}) == {"a": 1}
    assert jsonable_encoder_zulu(None) is None


def test_nested_datetime_in_dict():
    payload = {"created_at": datetime(2026, 9, 9, 1, 15, 0), "name": "x"}
    out = jsonable_encoder(payload, custom_encoder={datetime: jsonable_encoder_zulu})
    assert out["created_at"] == "2026-09-09T01:15:00Z"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd backend && python -m pytest tests/test_json_encoder.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.json'`）

- [ ] **Step 3: 实现 `backend/app/json.py`**

```python
"""docker-timezone：全局 UTC+Z 时间序列化。

FastAPI 在构造响应类之前已通过 jsonable_encoder 把 datetime 编码为字符串，
因此补 Z 的拦截点必须在 encoder 层而非响应 render 层。本模块提供
jsonable_encoder 的包装函数：naive datetime 按 UTC 解释追加 Z，
aware datetime 归一为 UTC+Z；其余类型原样交给原函数。
"""
import datetime as _dt

from fastapi.encoders import jsonable_encoder as _orig_jsonable_encoder


def jsonable_encoder_zulu(obj, *args, **kwargs):
    """datetime → UTC+Z ISO 字符串；其余类型委托原 jsonable_encoder。"""
    if isinstance(obj, _dt.datetime):
        if obj.tzinfo is None:
            return obj.isoformat() + "Z"
        return obj.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return _orig_jsonable_encoder(obj, *args, **kwargs)


def install_zulu_encoder() -> None:
    """替换 fastapi.routing 模块的 jsonable_encoder 引用（幂等）。"""
    import fastapi.routing as routing

    routing.jsonable_encoder = jsonable_encoder_zulu
```

- [ ] **Step 4: 接线 `backend/app/main.py`**

在 `app = FastAPI(...)` 之前（约 line 100）加入：

```python
# docker-timezone：统一 UTC+Z 时间序列化（naive 补 Z / aware 归一 Z）
from app.json import install_zulu_encoder

install_zulu_encoder()
```

- [ ] **Step 5: `queue.py:_iso` 补 Z**

`backend/app/routers/queue.py:71-72`：

```python
def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None
```

- [ ] **Step 6: 运行测试验证通过**

Run: `cd backend && python -m pytest tests/test_json_encoder.py -v`
Expected: 5 项全部 PASS

- [ ] **Step 7: 回归 smoke（若环境可用）**

Run: `cd backend && python -m pytest tests/test_api_smoke.py tests/test_queue.py -q`
Expected: 通过或仅有与本次改动无关的既有失败（记录）；不得引入新失败

- [ ] **Step 8: 提交**

```bash
git add backend/app/json.py backend/app/main.py backend/app/routers/queue.py backend/tests/test_json_encoder.py
git commit -m "feat(backend): 统一 API 时间字段输出 UTC+Z 标注"
```

---

### Task 3: 前端东八区格式化（format.ts）

**Files:**
- Modify: `frontend/src/utils/format.ts:25-45`（formatTime/timeAgo）、`format.ts:297-308`（timeUntil）
- Test: `frontend/src/utils/format.test.ts`

**Interfaces:**
- Consumes: 无（format.ts 自包含，不新增依赖）
- Produces: `formatTime`（东八区 `YYYY-MM-DD HH:mm`）、`timeAgo`、`timeUntil` 显式东八区；对既有调用方签名不变

- [ ] **Step 1: 写失败测试**

在 `frontend/src/utils/format.test.ts` 追加（保持既有 describe 块不变）：

```ts
import { formatTime, timeAgo, timeUntil } from './format'

describe('东八区时间格式化（docker-timezone）', () => {
  it('naive UTC 输入按 UTC 解释并渲染东八区', () => {
    // 09:15 UTC → 17:15 东八区
    expect(formatTime('2026-09-09T09:15:00')).toBe('2026-09-09 17:15')
  })
  it('带 Z 输入解析为绝对时刻后渲染东八区', () => {
    expect(formatTime('2026-09-09T09:15:00Z')).toBe('2026-09-09 17:15')
  })
  it('带 +08:00 偏移输入渲染东八区一致', () => {
    expect(formatTime('2026-09-09T17:15:00+08:00')).toBe('2026-09-09 17:15')
  })
  it('空值与非法输入回退', () => {
    expect(formatTime(null)).toBe('—')
    expect(formatTime(undefined)).toBe('—')
    expect(formatTime('not-a-date')).toBe('not-a-date')
  })
  it('timeAgo 基于绝对时刻差，跨时区一致', () => {
    // 固定 now：2026-09-09T10:00:00Z，输入为 5 分钟前的绝对时刻
    const now = Date.parse('2026-09-09T10:00:00Z')
    const fiveMinAgoUtc = new Date(now - 5 * 60000).toISOString()
    expect(timeAgo(fiveMinAgoUtc, now)).toBe('5 分钟前')
  })
  it('timeUntil 已过期返回 null', () => {
    const now = Date.parse('2026-09-09T10:00:00Z')
    expect(timeUntil('2026-09-09T09:00:00Z', now)).toBeNull()
  })
})
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: FAIL（`timeAgo` 签名不匹配 / 断言不通过）

- [ ] **Step 3: 实现 format.ts**

`formatTime` 改为：

```ts
/** 无时区后缀的 ISO 按 UTC 解释（补 Z）；带后缀按绝对时刻解析 */
function parseTime(iso: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso)
  return hasZone ? new Date(iso) : new Date(iso + 'Z')
}

/** 东八区显式格式化（Intl timeZone=Asia/Shanghai），输出 YYYY-MM-DD HH:mm */
function formatCst(date: Date): string {
  const parts = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = parseTime(iso)
  if (Number.isNaN(d.getTime())) return iso
  return formatCst(d)
}
```

`timeAgo` / `timeUntil` 签名增加可选 `now` 参数（默认 `Date.now()`），内部用 `parseTime` 得到绝对时刻：

```ts
export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—'
  const diff = now - parseTime(iso).getTime()
  if (Number.isNaN(diff)) return iso
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return formatTime(iso).slice(0, 10)
}

export function timeUntil(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null
  const diff = parseTime(iso).getTime() - now
  if (Number.isNaN(diff) || diff <= 0) return null
  const min = Math.ceil(diff / 60000)
  if (min < 60) return `${min} 分钟后`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时后`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天后`
  return formatTime(iso).slice(0, 10)
}
```

注意：`timeAgo`/`timeUntil` 新增可选参数不破坏既有调用（默认 `Date.now()`）。

- [ ] **Step 4: 运行测试验证通过**

Run: `cd frontend && npx vitest run src/utils/format.test.ts`
Expected: 全部 PASS（含既有 episodeStatus 断言）

- [ ] **Step 5: 全量前端测试**

Run: `cd frontend && npm run test`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/utils/format.test.ts
git commit -m "feat(frontend): 时间格式化显式按东八区渲染"
```

---

### Task 4: 视图调用点核查与前端构建

**Files:**
- Verify: 9 个视图文件中的 formatTime/timeAgo/timeUntil 调用点（无需改动）

**Interfaces:**
- Consumes: Task 3 的统一入口
- Produces: 核查记录——所有时间展示点均走统一入口，无直接 `new Date` 渲染绕行

- [ ] **Step 1: grep 全仓调用点**

Run: `cd frontend && grep -rn "formatTime\|timeAgo\|timeUntil" src --include="*.vue" --include="*.ts" | grep -v "\.test\."`
Expected: 9 个文件均 import 自 `@/utils/format`（或相对路径 utils/format），无内联日期格式化；记录结果

- [ ] **Step 2: 核查绕行点**

Run: `cd frontend && grep -rn "new Date(" src --include="*.vue" | grep -v "test"`
Expected: 仅 `LogsView.vue` 的时长相对差计算（两端解析一致即正确，记录不改造）；`isFutureDate` 为日期粒度判定（不改）

- [ ] **Step 3: 前端构建**

Run: `cd frontend && npm run build`
Expected: `vue-tsc --noEmit` 无类型错误 + `vite build` 成功

- [ ] **Step 4: 提交（无代码改动则跳过）**

若 Step 2 发现必须改的绕行点，修复后提交；否则本任务无提交。

---

### Task 5: 全量验证与记录

**Files:**
- Verify: 全局

**Interfaces:**
- Consumes: Task 1-4 全部产物

- [ ] **Step 1: 后端全量单测**

Run: `cd backend && python -m pytest -q`
Expected: 通过或记录与本次改动无关的既有失败（不得新引入）

- [ ] **Step 2: 前端全量测试 + 构建（若 Task 3/4 已跑则跳过重复）**

Run: `cd frontend && npm run test && npm run build`
Expected: 全绿 + 构建成功

- [ ] **Step 3: compose 再验证**

Run: `docker compose config --quiet`
Expected: 退出码 0

- [ ] **Step 4: 记录构建证据**

Run: `comet state record-check docker-timezone build --command "cd frontend && npm run build" --exit-code 0`

- [ ] **Step 5: 勾选 tasks.md 全部任务**

编辑 `docs/openspec/changes/docker-timezone/tasks.md`，把 4 个分组的全部复选框勾选为 `[x]`（1.1/1.2/2.1/2.2/2.3/3.1/4.1）。

- [ ] **Step 6: 提交**

```bash
git add docs/openspec/changes/docker-timezone/tasks.md
git commit -m "docs(openspec): 勾选 docker-timezone 全部任务"
```

---

## 自检

1. **Spec 覆盖**：
   - Docker 容器时区（compose TZ）→ Task 1 ✓
   - 后端时间字段 UTC+Z 语义明确 → Task 2 ✓
   - 前端东八区显式格式化 → Task 3 ✓
   - 视图覆盖核查 → Task 4 ✓
   - 存储保持 UTC（non-goal）→ Global Constraints ✓
2. **占位符扫描**：无 TBD/TODO；所有代码步骤含完整实现。
3. **类型一致性**：`parseTime`/`formatCst` 为私有函数，`timeAgo`/`timeUntil` 新增可选 `now` 参数不影响既有调用；测试与实现签名一致。
