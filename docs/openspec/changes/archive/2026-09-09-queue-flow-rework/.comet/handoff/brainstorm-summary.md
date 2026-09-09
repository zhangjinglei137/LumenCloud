# Brainstorm Summary

- Change: queue-flow-rework
- Date: 2026-09-09

## 确认的技术方案

### 主流程（对标 n8n 影视下载.json，收敛为两队列简化）

```
统一巡检(全局60min,可配置) → 搜索缺失集+收集转存凭据(share_info)  →  落 TaskQueue(轻量巡检结果队列,扁平展示)
        ↓ 每产生新任务即触发(事件) + transfer job 每分钟兜底
DownloadQueue 按 FIFO 从 TaskQueue 取件(尚未生成 DQ 行者) → 容量判断(已用+在途+新任务 ≤ 容量)
        ↓ 不足 → quota_wait 排队；释放后续跑
转存链(save→落盘→改名) → 转存成功落盘后立即格式化 download_name
   → aria2 下载 → 下载完成 → NasTools 转移 → transfer.finished(webhook)
   → 按媒体库文件夹调用 Emby 扫描 refresh → library_check 确认 → done + 删夸克 + 释放容量 → 续跑
```

### 关键决策（用户已确认）

- D1 统一巡检：移除 per-media `scan_interval_minutes` 到期过滤与冷却语义；全局间隔（默认 60min，system_config 可调）遍历全部 tracking/downloading 影视。
- D2 TaskQueue 状态机简化：`pending`（巡检写入）→ `ready`（凭据收集完毕，可下发）→ 下载队列取走即清源/标记完成；**移除 unmatched + silent_until 2 天静默**；本轮失败记录，下轮巡检重试。
- D3 下载队列双轨取件：① 巡检入队触发 → 从 TaskQueue FIFO 取「尚未生成 DownloadQueue 行」生成 pending；② 已有 pending 走容量准入（CAS 抢占）。容量为唯一准入约束。
- D4 GID 来源校验降级：陌生 aria2 任务 → flow_error 通知 + 跳过本轮（不再整批 fail-closed 永久停摆）。
- D5 格式化名称时机：转存成功落盘后立即格式化（复用 `_format_download_name`），后续 aria2 out / quark_path / 转移 / 入库全程沿用；失败重试幂等。
- D6 Emby 扫描：`transfer.finished` 后调用 Emby `POST /Library/Refresh`（全库 scan，管理端认证；已核实 dev.emby.media 文档：官方无按媒体库文件夹单独扫描端点），失败降级轮询确认（node_attempt 重试与超时回退沿用）。
- D7 前端任务队列扁平化：QueueView 从影视分组树改为扁平列表（影视名 - SxxExx + 状态 + 操作），终态从列表剔除；保留 download tab。

## 关键取舍与风险

- [巡检内探测可能单轮耗时变长] → 巡检与下载解耦；失败本轮跳过下轮重试，无 2 天静默。
- [GID 校验降级可能放行双转存] → 告警保留 + 跳过本轮（非永久停摆）；双转存仅手动强启 n8n 时发生，事件可追溯。
- [Emby 扫描 API 契约版本差异] → 失败降级轮询确认，不影响主链路。
- [scan_interval_minutes 语义废弃（BREAKING）] → 字段保留兼容读取，设置页隐藏。
- [TaskQueue 历史积累] → 终态不展示（剔除语义），物理清理沿用 cleanup 任务。

## 测试策略

- 后端：test_scan_*（统一巡检、入队只写 TaskQueue）、test_transfer.py（GID 降级、容量准入、命名幂等）、test_library_check（Emby 扫描 + 确认）、test_queue（扁平 API 契约）、test_capacity（排队续跑）。
- 前端：npm run build + 手工核对扁平列表/完成剔除。
- 端到端：添加影视 → 统一巡检产出缺失集 → FIFO 容量准入 → 转存后格式化 → 下载 → nastools 转移 → Emby 扫描 → 入库 done → 释放容量续跑。

## Spec Patch

无（open 阶段 delta spec 已覆盖全部能力与验收场景）。