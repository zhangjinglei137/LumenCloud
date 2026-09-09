# Brainstorm Summary

- Change: media-detail-ui
- Date: 2026-09-10

## 确认的技术方案

### 布局（单列全宽）
- detail-header flex：左区海报+标题/状态标签/操作按钮
- header 右侧**紧凑设置控件组**（仅 admin）：单集大小上限（GB）+ 电影上限（GB，仅 movie）+ 巡检间隔（分钟）+ 保存按钮；窄屏（<768px）换行
- guest 不显示紧凑组（沿用「仅管理员可修改」语义）
- 主体移除 el-row 双列 → 集数状态区 + TMDB 全集网格占满全宽
- 删除转存队列区块（模板+样式）；store/类型字段保留（无其他引用，避免破坏后端契约）

### 集数状态区
- 去掉 el-table max-height=480 限制，配合分组导航浏览
- 名称列展示 TMDB 名称，缺失回退 SxxExx（替代现有 `—`）
- 四色图例保留

### 分组导航（仅剧集）
- 纯函数 `buildEpisodeGroups(total, pageSize=100)` → [{label,start,end}]
  - 350 → 1-100/101-200/201-300/301-350；100 → 1-100；99 → 1-99；0 空集 → []
- 总集数 = episode_state 中 episode_number 最大值（tmdb_episodes 数量兜底）
- 默认态全部展示；点击 tag 过滤 `episode_number ∈ [start,end]`
- episode_number 缺失/非数值行始终显示（不参与过滤）
- 电影（media_type=movie）不渲染分组导航

## 关键取舍与风险

- 多季剧 episode_number 每季复位 → 按数值分组跨季归组；spec 350 连续场景字面成立，接受此语义
- 转存队列删除已 grep 确认仅 MediaDetailView 使用
- 窄屏紧凑组换行不溢出

## 测试策略

- format.test.ts：buildEpisodeGroups（350/100/99/0 边界）、名称回退（有名/无名→SxxExx）
- `vitest` + `npm run build` 通过；手动验证 350 集分组导航/名称展示/header 布局/转存队列消失

## Spec Patch

无（现有 delta spec 场景已覆盖全部需求：移转存队列、header 并排、集数最大显示、分组导航、名称回退）