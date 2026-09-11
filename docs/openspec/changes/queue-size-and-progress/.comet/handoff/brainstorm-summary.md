# Brainstorm Summary

- Change: queue-size-and-progress
- Date: 2026-09-11

## 确认的技术方案

**D1 探测阶段：保持均摊估算兜底（已确认）**
- cloudSaver share-list 无单文件 size：上游源码 map 丢弃（QuarkService.ts:109-115，夸克官方接口本身返回 item.size）+ 生产版实测条目字段仅 `[fileId, fileIdToken, fileName, isFolder]`
- 客户端传参无法改变 → scan.py `_walk_share` 保持容错读取 + 均摊估算兜底（size_estimated=True）
- 已提 issue：jiangrui1994/CloudSaver#130（请求透传单文件 size）；将来上游透传后本项目客户端已容错，无需改动

**D2 转存/下载阶段：精确值写入（已确认）**
- 下载完成回填（aria2 totalLength）已存在（transfer.py `_complete_download`，持久化 file_size + 清 estimated）
- 增强：`/queue/download/progress` 响应增加 `total` 字段（aria2 totalLength）→ 下载中行实时显示精确大小；不引入 downloading 高频轮询写库

**D3 前端拆列（已确认）**
- 「进度 / 大小」混列 → 拆「进度」列（仅 downloading 进度条+速度）+「大小」列（全行 formatFileSize，downloading 行优先 live total）
- 巡检队列大小列保持 formatFileSize（已条件化）

**D4 formatFileSize 语义（已确认）**
- fileSize null/undefined/≤0 → 「—」（0 值不算文件大小，防「0 B」误导）
- estimated && size>0 → 「约 X」；否则精确值

## 关键取舍与风险

- 探测阶段精确大小不可得的现实约束 → 估算「约」标注为常态兜底；精确主来源 aria2 totalLength（下载即知）
- 不做转存后 alist 路径回填（路径→文件映射复杂、与 aria2 total 重叠，YAGNI）
- progress 接口 total 缺失（aria2 故障）→ 降级 file_size(estimated)，前端已有静默降级机制

## 测试策略

- 后端 pytest：progress total 字段（totalLength→total，缺失 None 降级）；现状固化——walk_share 估算标记、transfer 完成回填清 estimated、列表 size_estimated 透传
- 前端 vitest：formatFileSize 四态（null/0/estimated/精确）；下载队列拆列渲染（downloading 行进度+大小、total 优先；非下载中仅大小）
- 集成：npm run build + 起服目视巡检/下载队列

## Spec Patch

1. delta spec 补充「下载中行大小」场景：WHEN downloading 且 aria2 totalLength 已知 → 大小列显示精确值（无「约」），优先于估算 file_size
2. 「大小缺失」场景澄清：file_size ≤ 0 或无值 → 显示「—」
3. 「下载后真实值回填」场景已实现（transfer），无缺口