---
comet_change: media-detail-ui
role: technical-design
canonical_spec: openspec
archived-with: 2026-09-10-media-detail-ui
status: final
---

# Design Doc: media-detail-ui — 影视详情页信息层级与集数浏览设计

## 1. 背景与目标

影视详情页（`frontend/src/views/MediaDetailView.vue`）当前存在以下问题（见 `docs/openspec/changes/media-detail-ui/proposal.md`）：
1. 转存队列区块与巡检/下载队列功能重叠，冗余
2. 大小上限、巡检设置分散在详情页右列表单中，与主信息层级分离
3. 集数状态区空间受限（右列挤占 + 表格 max-height 裁剪），全集数查找困难
4. 集数状态行无法直观看到 TMDB 集名称

目标（Non-Goals 之外的，见 design.md §Goals）：重设计详情页信息层级，令集数状态主导展示、集数浏览高效（100 集分组导航）、header 右侧集中设置控件；纯前端变更，不改后端 API 契约。

## 2. 现状数据流（已核实）

### 2.1 详情接口契约（`backend/app/routers/media.py:527` `get_media`）

- `episode_state`：有 `tmdb_episodes` 时输出**合并全集轴视图**（`merged_episodes`，每集含 `season / episode_number / name / air_date / in_emby / state / status / file_size / size_gb / file_name / updated_at`）；无 TMDB 数据时回退 `_dq_episode_dto` / `_episode_dto`（旧行，无 `name` 字段）
- **关键事实**：`merged_episodes` 每行**已携带 `name`**（来自 `tmdb.get_episode_info`/`get_tv_all_episodes` 的 `ep.name`，`media.py:664`），前端 `MediaDetailView` 的「名称」列当前已能显示该值（缺失显示 `—`）
- `transfer_queue`：`_task_queue_dto` / `_tq_dto` 摘要（移除详情页展示**不影响**后端契约，后端照常返回，前端不再渲染）
- `tmdb_episodes`：TMDB 全集轴（`season/episode/air_date/name/in_emby`，已按 season/episode 升序兜底排序后展示）

前端类型契约（`frontend/src/types/index.ts`）：`MediaDetail` 含 `episode_state?: EpisodeState[]`、`transfer_queue?: QueueSummaryItem[]`、`tmdb_episodes?: TmdbEpisode[]`。`EpisodeState` 与 `TmdbEpisode` 均带 `[key: string]: unknown` 索引签名，**新增字段无需改类型契约**（名称列已通过 `ep.name ?? '—'` 读取）。

### 2.2 Blast radius 核实（该 change 影响面）

- `MediaDetailView.vue`：本 change 唯一核心改动文件
- `format.ts`：新增 2 个纯函数（分组生成、名称回退），不修改既有导出
- `transfer_queue` 前端引用：`grep` 确认仅 `MediaDetailView.vue:95,305,310` 使用 queue-list 区块；无其他页面/组件消费 `MediaDetail.transfer_queue`（`SettingsView.vue` 的 `listEmbyLibrariesApi` 与 `embyLibraries` 是 Emby 媒体库，与 transfer_queue 无关）
- 移除后 `QueueSummaryItem` / `formatBytes` 的 import 需清理（`formatBytes` 若无其他引用则移除，`queue-status` 相关工具若仍被 QueueView 使用则保留——grep 确认后决定）

## 3. 技术设计

### 3.1 布局：detail-header flex + 紧凑设置控件组

**现状**：`.detail-header` 已是 flex（`MediaDetailView.vue:362`），左「海报」右「info（标题/状态/操作）」；设置表单与转存队列在主体右列。

**改后**：
- header 保持 flex；`.info` 区（标题/状态标签/最近扫描/操作按钮）不变
- header 右侧新增 `.header-settings` 紧凑块（仅 `auth.isAdmin` 渲染）：
  - 控件：单集大小上限（`el-input-number`，单位 GB）、电影上限（`v-if media_type==='movie'`）、巡检间隔（分钟）、保存按钮
  - 复用现有 `form` ref 与 `saveSettings()`（`MediaDetailView.vue:35,124`），表单字段名不变
  - **guest**：不渲染该块（沿用「仅管理员可修改」语义，Error 拦截已有）
- **响应式**：`.detail-header { flex-wrap: wrap }`；`@media (max-width: 768px)` 时 `.header-settings` 占满整行（`flex-basis: 100%`），控件横排自动换行；标题区不被挤压
- 原「大小与巡检设置」lc-panel 面板（`MediaDetailView.vue:324-354`）**删除**：其字段全部并入 header 紧凑组（电影上限仅 movie 出现），不留残余面板
- 转存队列 lc-panel（`:302-321`）**删除**：模板 + `.queue-list/.queue-item` 样式 + `transfer_queue` 相关 import 清理

### 3.2 主体：单列全宽

**现状**：`el-row :gutter="16"`，左 `<el-col :md="14">`（影视状态/集数状态+TMDB 全集），右 `<el-col :md="10">`（转存队列+设置面板）。

**改后**：移除 `el-row/el-col` 双列结构，主体改为单列全宽：
- `media_type === 'movie'`：保留「影视状态」lc-panel（状态标签 + 说明文案）
- `media_type !== 'movie'`：集数状态区 lc-panel 全宽展示
  - 集数状态区内容自上而下：四色图例 → **分组导航 tag 组**（新增）→ 集数状态表格（`max-height` 取消或大幅提高）→ TMDB 全集网格（保留完整展示，置表格下方）
  - 说明：TMDB 全集网格原先在表格上方（`:238-254`），与表格同列挤压。改为**置于表格下方**，让「集数状态表格」在最上方获得优先视区；分组导航紧贴图例便于先过滤后浏览
- 电影不渲染分组导航与 TMDB 全集网格（现状已如此：`movie` 分支只有影视状态面板）

### 3.3 集数分组导航

**新增纯函数**（`frontend/src/utils/format.ts`，与现有工具同风格、可为测试注入依赖格式）：

```ts
export interface EpisodeGroup {
  label: string   // '1-100' / '101-200' ... 末组 '301-350'
  start: number
  end: number
}

/** 按每 100 集生成分组；total<=0 → []。末组 end=total（非整百时收缩）。 */
export function buildEpisodeGroups(total: number, pageSize = 100): EpisodeGroup[]
```

边界：
- `total = 350` → `['1-100','101-200','201-300','301-350']`
- `total = 100` → `['1-100']`
- `total = 99` → `['1-99']`
- `total <= 0`（数据缺失）→ `[]`

**视图逻辑**（`MediaDetailView.vue` script）：
- `episodeNumberMax = computed(() => episodes.map(e => Number(e.episode_number)).max 过滤 NaN)`；兜底 `tmdbEpisodes.length`（当前 merged 视图 `episode_state` 与 `tmdb_episodes` 同源，最大值一致；无 episode_state 时用 tmdb 全长）
- `groups = computed(() => buildEpisodeGroups(episodeTotal))`
- `activeGroup = ref<EpisodeGroup | null>(null)`（默认 null = 全部）
- `filteredRows = computed(() => activeGroup.value ? episodeRows.filter(r => {
    const n = Number(r.episode_number);
    if (!Number.isFinite(n)) return true;            // 无集号行始终显示
    return n >= g.start && n <= g.end;
  }) : episodeRows)`
- template：`el-radio-group` 或 `el-tag` 组渲染 groups（`v-if groups.length > 0 && media_type !== 'movie'`），含「全部」按钮（`activeGroup=null`）；radio-group 语义更贴合单选过滤，选定按 key（`${start}-${end}`）

### 3.4 名称列回退

**现状**：`episodeName()`（`MediaDetailView.vue:85`）返回 `ep.name` 或 `'—'`。

**改后**：`episodeName(ep)` 无名称时回退集号 `SxxExx`（复用 `episodeLabel(ep)` 结果），即：
```ts
function episodeName(ep) {
  const name = ep.name
  if (typeof name === 'string' && name !== '') return name
  return episodeLabel(ep)   // 集号；仍无 → '—'（理论上不出现）
}
```
- 名称列标题不变（「名称」）；有名称显示名称，无名称显示集号（不再出现裸 `—`）
- 该逻辑浓缩为新纯函数 `episodeDisplayName(row)` 放入 format.ts 以便单测（名称 + 集号回退两路径）

### 3.5 边界条件与降级

| 场景 | 行为 | 验证 |
|------|------|------|
| 电影（media_type=movie） | 无分组导航、无 TMDB 网格、无集数表格（现状）；header 紧凑组显示电影上限 | 电影详情页正常 |
| 无 episode_state / 无 TMDB | 分组为空数组 → 不渲染分组导航；表格 el-empty 兜底；集数状态区标题 `（0）` | 空数据不报错 |
| episode_number 缺失/NaN | 行始终显示（不过滤），名称回退集号逻辑不依赖该字段 | 遗留行可见 |
| guest 角色 | header 紧凑组不渲染；`saveSettings` 不可达 | admin/guest 差异 |
| TMDB 全集网格为空（tmdb_episodes 省略） | 网格 div 不渲染（`v-if tmdbEpisodes.length > 0`，现状保留）；表格仍按 episode_state 显示 | 外部服务故障降级 |
| 前端构建 | `transfer_queue` import 清理后无 unused 报错；`npm run build` 通过 | — |

## 4. 实施任务映射（对应 tasks.md）

| tasks.md | 设计落实 |
|----------|----------|
| 1.1 header flex + 右侧并排 | §3.1（紧凑控件组 = 单集上限/电影上限/巡检间隔 + 保存） |
| 1.2 移除转存队列 | §3.1 区块删除 + import 清理（已 grep 确认无其他依赖） |
| 2.1 集数状态最大显示 | §3.2 单列全宽 + 表格去 max-height + TMDB 网格下移 |
| 2.2 分组 computed | §3.3 buildEpisodeGroups + groups computed |
| 2.3 分组过滤 | §3.3 activeGroup + filteredRows |
| 3.1 集名称映射/回退 | §3.4 episodeDisplayName |
| 4.1 vitest | format.test.ts 新增用例（分组边界 + 名称回退） |
| 4.2 手动验证 + build | §3.5 降级矩阵 |

## 5. 测试策略

### 5.1 单元测试（`frontend/src/utils/format.test.ts` 追加，vitest）
1. `buildEpisodeGroups`：
   - 350 → 四组，末组 `start=301,end=350`
   - 100 → 单组 `1-100`；99 → 单组 `1-99`（末组收缩）
   - 0 / 负数 / 非数 → `[]`
   - 200 → 两组；(100+1) 边界 101 → `['1-100','101-101']`
2. `episodeDisplayName`：
   - 有 name → 返回 name
   - 无 name 但有 season/episode_number（如 `{season:1,episode_number:4}`）→ `'S01E04'`
   - 无 name 且不可解析 → `'—'`

### 5.2 构建与手动验证
- `npm run build`（frontend)零报错
- 手动：350 集剧集详情 → 分组 tag 出现、「101-200」点击后表格仅 100 行、清回「全部」恢复全量；集数名称展示/回退；电影详情无分组；header 右侧紧凑组与保存生效；转存队列区块消失；guest 登录看不到设置组

## 6. 风险与权衡

| 风险 | 缓解 |
|------|------|
| 多季剧 episode_number 复位（第 2 季又从 1 开始）→ 按数值分组跨季归组 | 被接受的 spec 语义：proposal 明确 350→100 场景为连续数字分组；若未来需要季维度分组再加字段，不在本 change 范围 |
| 移除转存队列误伤 | 已 grep 确认全前端仅 MediaDetailView 引用 `transfer_queue`；删除后无编译残留 |
| name 字段在旧行回退 DTO 缺失 | `episodeDisplayName` 回退集号，永不显示裸 `—`；旧的 `_dq_episode_dto` 无 name 时该列显示 SxxExx，语义可读 |
| 表格全量无 max-height + 未分组全显（如 3000 集） | 分组导航默认「全部」时仍全量渲染；el-table 虚拟滚动不在范围，改为保留一个较大 max-height（如 600px）+ 滚动条，兼顾「最大显示」与性能 |

## 7. 非目标（重申）

- 不改后端 API 契约（`episode_state`/`tmdb_episodes`/`transfer_queue` 照常返回）
- 不改队列运维能力（queue-inspection-rework）
- 不改集数状态判定逻辑（episode-status-cache 职责）
- 不做季维度分组、不做表格虚拟滚动

## 8. Open Questions

无（brainstorming 三轮决策已闭合，见 `brainstorm-summary.md`）
