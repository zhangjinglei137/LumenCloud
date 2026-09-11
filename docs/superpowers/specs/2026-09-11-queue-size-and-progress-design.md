---
comet_change: queue-size-and-progress
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-11-queue-size-and-progress
status: final
---

# Design Doc: queue-size-and-progress（队列大小精确值与下载进度分区展示）

> 本文是 open 阶段 `design.md`（高层方案框架 D1-D3）的深度技术细化，覆盖现状核查结论、详细实现设计、数据流、边界条件与测试策略。OpenSpec delta spec（`queue-inspection-display`）为需求契约事实源。

## 1. 背景与目标

巡检队列每行大小显示「约 xxGB」（`size_estimated` 估算值加「约」前缀），用户要求显示精确值；下载队列下载中单元格把进度条 + 速度与文件大小混排（按状态二选一），需要分区展示。详见 `proposal.md` Why。

## 2. 现状核查结论（代码事实）

### 2.1 数据流现状

| 链路 | 位置 | 行为 |
|------|------|------|
| 探测入队 | `scan.py _walk_share`(540) / `_enqueue`(1213) | cloudSaver share-info 解析分享总大小（容错字段 fileSize/file_size/size/totalSize，573）；share-list 递归遍历，单文件 size 容错读取 `it.size`/`it.fileSize`；缺失（≤0）→ 按「分享总大小 / 文件数」均摊估算并置 `size_estimated=True` |
| 下载完成回填 | `transfer.py _complete_download`(534) | aria2 `tell_status.totalLength` 非 0 → `file_size=real_size, size_estimated=False`（条件更新 downloading→scrape，幂等；546-549 行注释明确避免回填 0） |
| 列表下发 | `queue.py _list_flat`(152) / `_list_download`(215) | `file_size` + `size_estimated` 全量透传（巡检 + 下载视图）；share_code/share_url 仅 admin 明文 |
| 实时进度 | `queue.py download_progress`(831) | 返回 `{id, gid, speed, progress, download_name}`；**无 total 字段**；aria2 故障降级 null 字段 |
| 前端巡检 | `QueueView.vue` 大小列(376) | `formatFileSize(row.file_size, row.size_estimated)` 条件渲染（已正确） |
| 前端下载 | `QueueView.vue`「进度/大小」列(602) | downloading → 进度条+速度（**无大小**）；非 downloading → `formatBytes(file_size)`（**无「约」标注**） |

### 2.2 外部依赖事实（cloudSaver 单文件大小调研）

- 上游 `QuarkService.getShareList`（`QuarkService.ts:109-115`）调用夸克官方 `sharepage/detail` 后 **map 仅保留 fileId/fileName/fileIdToken，主动丢弃 `item.size`**；`fileSize` 仅顶层总大小（`data.share?.size`）。
- 夸克官方接口列表条目**本身含 size**：boxplayer（`mapShareFile` 读 `item.size`）、MoonTVPlus（转存前后大小匹配）均在消费。
- **生产版实测**（分享码 fce1ba10fa28，只读调用）：条目字段集合仅为 `['fileId', 'fileIdToken', 'fileName', 'isFolder']`，无单文件 size；顶层 `fileSize` 仅总大小（约 107GB）。
- 结论：**探测阶段单文件精确大小当前不可得**；客户端传参无法改变（服务端 map 丢弃）。已向上游提交 issue：[jiangrui1994/CloudSaver#130](https://github.com/jiangrui1994/CloudSaver/issues/130)（请求透传 size）。本项目客户端 `_walk_share` 已容错读取，上游透传后自动生效、无需改动。

## 3. 设计决策

### D1 探测阶段：保持均摊估算兜底

- `_walk_share` 保留现有逻辑（容错读取 size/fileSize → 缺失时均摊估算 + `size_estimated=True`）。
- 不臆造精确值：估算值展示为「约 X」，属兜底而非常态（对齐 delta spec「估算大小标注」场景）。
- 上游 issue #130 落地后无需本项目改动（客户端已容错）。

### D2 下载中行实时精确：`/queue/download/progress` 增加 `total` 字段

- `download_progress`（queue.py:831）在 `tell_status` 成功且 `totalLength > 0` 时，`entry["total"] = totalLength`；缺失/非法/0 → `None`（与现有 progress/speed 的降级语义一致）。
- 前端：下载中行大小 = `progressMap[id].total ?? row.file_size`：
  - `total` 存在 → `formatBytes(total)`（精确值，无「约」）；
  - 否则 → `formatFileSize(file_size, size_estimated)`（估算兜底「约 X」）。
- **不引入 downloading 高频轮询写库**：前端 2.5s 轮询 `fetchProgress` 期间若后端落库回填，将产生 2.5s × downloading 行数的无关写放大；实时展示 + 下载完成时持久化回填（现状已有）即可覆盖需求。转存后 alist 路径回填同样否决（路径→文件映射复杂、与 aria2 totalLength 信息重叠，YAGNI）。
- 数据一致性：`total`（实时）与 `_complete_download` 回填值同源（均来自 aria2 `totalLength`），展示与落库无矛盾。

### D3 下载队列拆列

- 现状「进度 / 大小」单列（602-616，按状态二选一渲染）拆为两列：
  - **进度列**：仅 `downloading` 渲染进度条 + 速度（现状模板复用）；其他状态显示「—」（对齐 delta spec「非下载中状态」：不显示进度条）。
  - **大小列**：所有行渲染，下载中行 `total ?? file_size`（见 D2），其余行 `formatFileSize(file_size, size_estimated)`。
- 巡检队列大小列保持 `formatFileSize(row.file_size, row.size_estimated)`（已条件化，仅验证）。

### D4 `formatFileSize` 语义调整

```
formatFileSize(fileSize, estimated?):
  fileSize == null | undefined | <= 0  → '—'
  s = formatBytes(fileSize)
  estimated ? `约 ${s}` : s
```

- 「≤0 视为无记录」：影视场景不存在 0 字节文件（后端 `_complete_download` 已刻意避免回填 0，477 行注释），0 显示「0 B」会误导为空文件 → 统一「—」，对齐 delta spec「大小缺失」场景。
- 影响面：调用处仅 `QueueView.vue`（巡检 + 下载大小列）；`formatBytes` 保持字节级展示供其他调用（容量、quota 提示等）。

## 4. 边界条件

| 条件 | 行为 |
|------|------|
| aria2 故障 / tell_status 字段缺失 | `total`/`progress`/`speed` 降级 null；下载中行大小回退 `file_size(estimated)`；前端已有静默降级（fetchProgress 失败不打断轮询） |
| `totalLength = 0`（0 字节） | `total = None`，不显示「0 B」，回退 file_size |
| 历史任务无真实大小 | 估算值 +「约」标注；新任务下载链路即回填精确值 |
| 非 admin 访问 | 大小展示不受影响（share_code/share_url 脱敏与本 change 正交） |
| 上游 cloudSaver 透传 size（issue #130 落地） | `_walk_share` 容错读取自动生效，探测入队即精确，估算仅余无 size 的异常分享 |

## 5. 测试策略

### 5.1 后端（pytest，`backend/tests/test_queue.py` 扩展）

- `download_progress` 含 `total`：totalLength>0 → total=值；缺失/非法/0 → None（降级）。
- 现状固化（回归，防重构破坏）：
  - `_walk_share`：单文件 size 缺失 → 均摊估算 + `size_estimated=True`；size 存在 → 直接用且不标估算。
  - `_complete_download`：real_size 非 None → 回填 file_size + 清 estimated；real_size None → 不回填。
  - 列表接口（`_list_flat` / `_list_download`）透传 `size_estimated`。

### 5.2 前端（vitest，`frontend/src/utils/format.test.ts` + `QueueView.test.ts` 扩展）

- `formatFileSize` 四态：null →「—」；0 →「—」；estimated=true →「约 X」；精确 → 无前缀。
- 下载队列拆列渲染：
  - downloading 行：进度条 + 速度（进度列）、大小列优先 `progressMap.total`；
  - 非 downloading 行：无进度条，仅大小列；
  - total 缺失：大小列回退 `file_size(estimated)` 带「约」。

### 5.3 集成

- `npm run build`（frontend）零报错；本地起服目视巡检/下载队列（下载中行大小精确、进度独立列、「约」仅估算兜底、0/空显示「—」）。

## 6. Spec Patch（已回写 delta spec）

1. **新增场景「下载中行精确大小」**（ADDED Requirement「下载队列展示进度与大小」内）：WHEN 任务处于下载中且 aria2 totalLength 已知 → 大小独立列展示精确值（不带「约」），优先于估算 file_size。
2. **「大小缺失」场景澄清**：WHEN 某任务尚无文件大小记录（file_size 为空或 ≤ 0）→ 显示「—」，不填充虚假值。
3. **「下载后真实值回填」场景确认已实现**（transfer.py `_complete_download`，无缺口）。

## 7. 变更面汇总

- 后端：`backend/app/routers/queue.py`（`download_progress` 增加 total 字段）；测试 `backend/tests/test_queue.py` 扩展。
- 前端：`frontend/src/utils/format.ts`（`formatFileSize` ≤0 语义）；`frontend/src/views/QueueView.vue`（下载队列拆列、大小列 total 优先）；测试 `format.test.ts` / `QueueView.test.ts` 扩展。
- 无新增依赖、无数据库变更、无迁移（对齐 design.md Migration Plan）。
- 外部依赖：cloudSaver 透传单文件 size 依赖上游 issue #130，非本 change 阻塞项。
