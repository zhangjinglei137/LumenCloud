---
comet_change: docker-timezone
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-09-docker-timezone
status: final
---

# Docker Timezone 深度技术设计

> 对应 OpenSpec change：`docker-timezone`（`docs/openspec/changes/docker-timezone/`）
> 语言：zh-CN　日期：2026-09-09　状态：已由用户确认

## 1. 背景与目标

用户反馈 Docker 部署下即使配置了 `TZ: Asia/Shanghai`，容器/应用展示时间仍非东八区。根因核查（代码级）：

- **Docker 层**：`docker-compose.yml` 与 `docker-compose.prod.yml` 的 `lumencloud` / `db` 服务均未声明 `TZ` 环境变量（用户自行添加未生效，或加到了未持久化的 compose override）。
- **后端**：`now_utc_naive()`（`backend/app/utils.py:20`）统一产生 naive UTC；models 的 `server_default=CURRENT_TIMESTAMP`（PG 存 UTC）；路由返回时由 FastAPI 默认序列化 naive datetime → **无时区后缀 ISO**（如 `2026-09-09T01:15:00`），浏览器 `new Date(iso)` 按本地时区误解析。
- **前端**：`format.ts:25-45` 的 `formatTime` / `timeAgo` / `timeUntil` 均基于 `new Date(iso)`（浏览器本地时区）渲染；9 个视图文件 29 处调用点全部走统一入口。

**目标**：所有时间展示统一为东八区（Asia/Shanghai），跨浏览器/跨时区一致；容器内系统时间 = CST。

**非目标**：数据库迁移/时间重算（存储保持 UTC）；APScheduler 调度语义改动；历史数据单独修正（统一换算即覆盖存量）。

## 2. 方案总览（已确认）

三层横切改动：

| 层 | 动作 | 落点 |
|----|------|------|
| Docker | 注入 `TZ: Asia/Shanghai` | docker-compose.yml / docker-compose.prod.yml（lumencloud + db） |
| 后端 | 统一 UTC+Z 标注输出 | 新增全局序列化 handler + `queue.py:_iso()` 补 Z；存储不变 |
| 前端 | 显式东八区格式化 | `format.ts` 统一解析层 + `Intl timeZone=Asia/Shanghai` 渲染 |

关键决策：**后端采用「补 Z」方案**（用户拍板）。spec.md 原文「后端返回时间字段明确标注时区」保持成立，不采用 design.md 早期修订的「前端契约方案」。

## 3. 详细设计

### 3.1 Docker 层

`docker-compose.yml` 与 `docker-compose.prod.yml`：

- `db` 服务 `environment` 增加 `TZ: Asia/Shanghai`
- `lumencloud` 服务 `environment` 增加 `TZ: Asia/Shanghai`

验证：`docker compose config` 解析通过（语法级）；运行时 `date` 显示 CST（手动验证项，用户部署后确认）。

### 3.2 后端：统一 UTC+Z 序列化

**原则：不逐字段手改**（52 处 dict 序列化点，逐字段违背「无漏网」要求），在序列化边界做全局归一。

**方案**：在 FastAPI 编码边界做统一拦截——自定义 `jsonable_encoder` 包装函数（拦截点必须在 jsonable_encoder 层，因为 FastAPI 在构造响应类**之前**已把 datetime 编码为字符串，JSONResponse.render 阶段无法再区分 naive/aware）：

```python
# backend/app/json.py
import datetime as _dt
from fastapi.encoders import jsonable_encoder as _orig_jsonable_encoder

def jsonable_encoder_zulu(obj, *args, **kwargs):
    # datetime 归一为 UTC+Z：naive 按 UTC 解释补 Z，aware 转 UTC 归一为 Z
    if isinstance(obj, _dt.datetime):
        if obj.tzinfo is None:
            return obj.isoformat() + "Z"
        return obj.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return _orig_jsonable_encoder(obj, *args, **kwargs)
```

`_zulu_default(obj)` handler 规则：
- `datetime` 且 `tzinfo is None`（naive）→ `obj.isoformat() + 'Z'`（按 UTC 解释）
- `datetime` 且 aware → `obj.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')`（归一 UTC+Z）
- 其余类型回退标准序列化

**接线**（`main.py` 中 `app = FastAPI(...)` 之前）：
- `import fastapi.routing`，将 `fastapi.routing.jsonable_encoder` 替换为包装函数 → 覆盖所有 `return {...}` dict 路由的 datetime 编码
- `queue.py:_iso()` 改为 `dt.isoformat() + 'Z'`（显式序列化点，与全局 handler 语义一致）
- 既有显式 `JSONResponse(...)` 调用（如 `main.py:139` 503 分支）若含 datetime 字段，直接改用 aware UTC 输入或保持语义一致（health 已输出 `+00:00`，可归一为 Z）

**核查记录**（build 阶段核对，无代码改动预期）：
- `main.py:132` health：`datetime.now(timezone.utc).isoformat()` 已输出 `+00:00`（aware），语义正确，可不改（或顺手归一为 Z，二选一在 build 定）
- `logs.py` / `media.py` / `approvals.py` / `admin.py` / `capacity.py`：均塞原始 datetime 进 dict → 由全局 handler 覆盖
- `tasks/` 与 `services/` 内部 `isoformat()`（如 scan.py、nastools_sync.py）：写配置/内部存储，非 API 响应面，不动

**存储语义**：`now_utc_naive()`、`server_default=CURRENT_TIMESTAMP` 全部保持不变（naive UTC），无迁移、无数据重算。

### 3.3 前端：统一东八区格式化（核心）

改造 `frontend/src/utils/format.ts`：

**解析层**（新增私有 helper）：

```ts
/** 无时区后缀的 ISO 按 UTC 解释（补 Z）；带后缀按绝对时刻解析 */
function parseTime(iso: string): Date {
  // 匹配结尾 Z / +08:00 / +0800 形态
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso)
  const d = hasZone ? new Date(iso) : new Date(iso + 'Z')
  return d
}
```

**渲染层**（东八区显式）：

```ts
const fmtCST = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
})
```

`formatTime` 改造：
- 空值 → `'—'`（不变）
- 非法日期 → 原样返回（不变）
- `parseTime(iso)` → `fmtCST.format(d)` → 规范化 `YYYY-MM-DD HH:mm`（去 `zh-CN` 的 `/` 分隔符与年份汉字前缀差异，build 时以现有测试断言锁定输出形态）

`timeAgo` / `timeUntil`：
- 改为 `parseTime(iso)` 得到绝对时刻，差值与展示时区无关，天然正确
- `timeAgo` 中 `>= 30 天` 回退分支已复用 `formatTime`，自动东八区

**视图层**：29 处调用点全部走 `format.ts` 统一入口，一处改造全量生效，**不逐视图改动**。build 阶段 grep 复查确认无新增绕行点。

**绕行点记录**：
- `LogsView.vue:50`：`new Date(finished_at) - new Date(started_at)` 为相对差（两端解析一致即正确，不涉展示时区），仅核查，不改造。

**时区探测约束**：`isFutureDate`（`format.ts:215`）使用本地 0 点粒度比较，语义是「日期粒度」而非时刻展示，**不改**（避免影响集数状态判定）。

### 3.4 Spec Patch（回写 delta spec）

`specs/timezone-display/spec.md`「后端时间字段时区语义明确」：
- 措辞从「带 +08:00 的 ISO 或明确 UTC」收敛为「统一输出 UTC+Z 标注（`isoformat` + `Z`）」
- 保持 storage-UTC 场景与展示场景不变
- 目的：消除「后端可任选标注」的歧义，与补 Z 方案、design 决策一致

### 3.5 测试策略

`frontend/src/utils/format.test.ts` 补充：
- `formatTime` 东八区固定输出断言：
  - naive 输入 `'2026-09-09T01:15:00'`（无后缀）→ 期望 `2026-09-09 09:15`
  - 带 Z 输入 `'2026-09-09T01:15:00Z'` → 期望 `2026-09-09 09:15`
  - `null` / `undefined` → `'—'`
- `timeAgo` 跨时区一致性：固定 `Date.now()`（vi.mock / 参数注入）下，同一绝对时刻输入输出一致
- `timeUntil` 边界：已过期 → null；未来 → 正确的「X 分钟后/后小时」

验证命令：`vitest`（frontend）、`npm run build`。

## 4. 风险与取舍

| 风险 | 缓解 |
|------|------|
| 全局 JSONResponse 改变所有路由响应格式 | 仅 datetime 归一化，其他类型不动；旧前端解析带 Z 的 ISO 本就正确（`new Date` 支持），兼容 |
| `zh-CN` Intl 输出形态（`2026/09/09 09:15`）与现有 `YYYY-MM-DD HH:mm` 不一致 | 渲染层对 Intl 输出做规范化拼装，测试断言锁定；或改用手动 `getUTCHours()` 换算（东八区无 DST，+8h 恒定，`(d.getTime() + 8h)` 后 getUTC* 也是可行备选） |
| naive 解析补 Z 后，存量数据展示位移 | 预期行为：统一按 UTC 语义展示，全部视图一致，不单独修历史 |
| FastAPI `default_response_class` 不覆盖显式 `JSONResponse` 分支 | 显式分支逐一替换为 `ZuluJSONResponse`（核查清单覆盖） |
| 浏览器 Intl 兼容性 | 现代浏览器（含移动端）均支持 `Intl.DateTimeFormat` timeZone；Vite 目标即 ES2020+ |

## 5. 边界条件

- **后端输入侧**：API 请求体中的时间字段（如 approvals reviewed_at 写入）用 `_now()` naive UTC，不涉解析
- **`air_date`**：纯日期字符串（`YYYY-MM-DD`），`isFutureDate` 走本地 0 点粒度，**不经 formatTime**，不受影响
- **health check**：返回 `time` 字段带 `+00:00`，前端无消费点，语义正确
- **test 套件**：既有 `format.test.ts` 断言（episodeStatus 等）不涉时间展示，不受影响；新增用例全绿为退出条件

## 6. 验证计划

1. `docker compose config` 通过（compose 语法 + TZ 注入生效）
2. `vitest` 全绿（含新增东八区用例）
3. `npm run build` 通过（前端类型/构建无回归）
4. 手动（用户部署后）：容器 `date` 显示 CST；前端队列/影视库/详情/日志时间 = 东八区；不同时区浏览器一致

## 7. 回滚

- 还原 `docker-compose*.yml` 的 TZ 行
- 还原 `format.ts` 为本地时区解析 + 还原自定义 JSONResponse
- 无数据库影响、无数据迁移
