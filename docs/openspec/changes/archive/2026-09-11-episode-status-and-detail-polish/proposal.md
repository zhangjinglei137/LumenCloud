# Proposal: episode-status-and-detail-polish

## Why

影视列表卡的「已有 0/xx 集」聚合逻辑缺失 Emby 实际入库维度，导致实际已在 Emby 的集数显示为 0，与真实状态严重不符；影视详情页缺少「当前进行中任务」区块，用户无法直观看到哪些集正在搜索/等待/下载；详情页「状态」下拉宽度过窄，选中后看不到选项文本。

## What Changes

- **列表卡集数统计改为「已有 N 缺失 M」**：已有 = Emby 实际入库集数 ∪ 已下载完成集数（去重），缺失 = TMDB 全集 - 已有；修复聚合逻辑导致的恒 0 问题
- **详情页新增「当前进行中任务」区块**：保留 TMDB 全集分组（全部 / 1-100 / 101-200 …）与集数状态区；新增进行中任务展示，覆盖搜索中、等待中、下载中（含转存中/刮削中/入库确认等全部进行中状态），完成/失败/跳过状态不展示；仅展示已到首播日且尚未入库的集（如 S01E157 已到首播日但不在 Emby → 展示；S01E158 未到首播日 → 不展示）
- **修复详情页「状态」下拉宽度**：显式设置下拉宽度，保证选中后选项文本完整可见

## Capabilities

### New Capabilities
<!-- 无新增 capability，全部归入既有能力修改 -->

### Modified Capabilities
- `media-status`: 影视列表集数统计真实展示（新增 Emby 实际入库聚合维度、「已有 N 缺失 M」展示语义）
- `media-detail-ui`: 影视详情集数状态展示（新增「当前进行中任务」区块与状态宽度）

## Impact

- 后端：`backend/app/routers/media.py`（`_stats` 聚合增加 Emby 入库维度、详情接口任务区块数据）、`backend/app/services/emby.py`（新增查询影视已入库集数能力）
- 前端：`frontend/src/views/MediaListView.vue`（集数文案）、`frontend/src/views/MediaDetailView.vue`（进行中任务区块、下拉宽度）、`frontend/src/utils/format.ts`（展示辅助）、`frontend/src/types/index.ts`（类型扩展）
- 依赖：Emby 服务端（按影视/库查询已入库集）；无数据库变更
