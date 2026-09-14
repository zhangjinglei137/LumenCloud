---
comet_change: fix-notification-templates
role: technical-design
canonical_spec: openspec
---

# Design Doc：修复通知模板

日期：2026-09-14

## 1. 背景与目标

见 change `docs/openspec/changes/fix-notification-templates/proposal.md`。本设计聚焦「如何」实现：统一通知文案、移除噪音通知、PushPlus HTML 美化、前端铃铛类型呈现。所有「是什么」约束与验收场景以 delta spec `specs/notifications/spec.md` 为准。

## 2. 架构决策

### 2.1 文案工厂（新增 `backend/app/services/notify_templates.py`）

设计动机：12 处通知生成点散布在 7 个模块，文案全部内联。引入单一工厂保证「媒体名 + SxxExx」「中文文案」规范不再漂移，且工厂可独立单测。

接口约定（所有函数返回 `tuple[str, str]` = `(title, body)`）：

```python
def download_complete(media_name: str, episode: str, is_movie: bool) -> tuple[str, str]
    # 剧集 title: f"入库完成：{media_name} · {episode}"
    # 剧集 body:  f"媒体 {media_name} · {episode} 已入库完成。"
    # 电影 title: f"入库完成：{media_name}"
    # 电影 body:  f"媒体 {media_name} 已入库完成。"

def approval_pending(title: str) -> tuple[str, str]
    # title: f"新的想看请求：{title}"
    # body:  由调用方拼去重前缀后追加（工厂接收前缀？否——调用方负责 f"{WR_PREFIX}{id} {title}"）

def flow_error_transfer(media_name: str, episode: str, reason: str, attempts: int) -> tuple[str, str]
def flow_error_capacity(rate_pct: float, used_gb: float, total_gb: float, consecutive: int, threshold_pct: float) -> tuple[str, str]
def flow_error_nastools_sync(exc: str) -> tuple[str, str]
def flow_error_nastools_event(chinese_name: str, media_title: str) -> tuple[str, str]
def flow_error_alert(title: str, message: str) -> tuple[str, str]  # 通用告警（transfer 流程/容量等已具名者之外的兜底）
```

关键约束：
- **去重前缀不属于工厂**：`notification_scan.py` 需要 body 以 `wr#{id} `/`tq#{id} ` 开头做查重。工厂返回纯文案，调用方按需在 body 首部拼接前缀。这样站内去重机制完全不变。
- **电影判定集中在调用方**：`library_check.py` 当 `media.media_type == "movie"` 时传 `is_movie=True`。工厂不做数据库判定，保持纯函数。

### 2.2 移除噪音通知

| 位置 | 事件 | 动作 |
|---|---|---|
| `approvals.py` approve 成功后「开始入库」 | download_started | 删除 notify 调用（保留 Emby 防重、media 入库、trigger_scan_background） |
| `transfer.py` addUri 成功后「下载开始」 | download_started | 删除 notify 调用（保留容量缓存失效） |
| `transfer.py` _after_complete_promote「下载完成」 | download_complete | 删除 notify 调用（**保留** `_spawn(scrape_runner)` 刮削触发） |

- `EVENT_DOWNLOAD_STARTED` 常量和 `EVENT_TYPES` 集合保持不变（避免破坏外部引用/测试），只是不再有调用点。
- `download_complete` 事件类型仍保留，语义收敛为「入库完成」（library_check `_finalize_done` 唯一调用点）。

### 2.3 入库完成文案（library_check.py）

- `library_check()` 主循环已取得 `media = await s.get(Media, media_id)`，将 `media.title`、`media.media_type == "movie"` 传入 `_finalize_done(...)`。
- `_finalize_done` 通知段改为调用 `notify_templates.download_complete(media.title, episode, is_movie)`。
- 原正文中的 `媒体 {media_id} · {episode}（{file_name}）已确认被 Emby 收录，夸克中转文件已释放。` 全部替换，不再含 media_id/file_name。

### 2.4 英文事件名映射（nastools_notify.py）

内部判定继续使用 `_EVENT_TRANSFER_FINISHED` / `_EVENT_TRANSFER_FAIL` / `_EVENT_DOWNLOAD_FAIL` 常量（webhook 契约不变），仅在用户可见文案做中文映射：

```python
_EVENT_TRANSFER_FINISHED  → "转存完成"
_EVENT_TRANSFER_FAIL      → "转存失败"
_EVENT_DOWNLOAD_FAIL      → "下载失败"
```

`_notify_flow_error` 的 title/body 改用映射后的中文名（配合文案工厂 `flow_error_nastools_event`）。

### 2.5 PushPlus HTML 模板

`PushPlusNotifier.notify` 增加纯文本→HTML 转换函数（新建 `backend/app/services/notify_templates.py` 内 `text_to_html(body, title)` 或同模块 helper）：

- 标题：包 `<b>…</b>`
- 正文：按 `\n` 拆段落，每段包 `<p>…</p>`
- 高亮：识别 `媒体 X · SxxExx`（正则 `(媒体\s*[^·\n]+·\s*S\d{2,3}E\d{2,3})`）与 `媒体 X`（位置靠前），对命中片段包 `<span style="color:#4caf50">…</span>`（入库完成类）或不加色（通用）。简化：统一对「媒体 …」片段加粗/着色，语义为聚焦媒体名与集数。
- 发送时 `template="html"` 传给 PushPlusClient.send。

站内 `body` 仍存纯文本（铃铛不渲染 HTML，避免 XSS/样式污染）。转换只发生在 PushPlus 通道出口。

### 2.6 前端铃铛类型映射

`frontend/src/utils/format.ts` 新增：

```ts
export interface NotificationTypeMeta {
  label: string
  icon: string      // element-plus icon 组件名
  color: string     // CSS 色值
}
export function notificationTypeMeta(event_type?: string): NotificationTypeMeta
// download_complete → { label:'入库完成', icon:'CircleCheckFilled', color:'#67c23a' }
// approval_pending  → { label:'待审批', icon:'BellFilled', color:'#409eff' }
// flow_error        → { label:'告警', icon:'WarningFilled', color:'#f56c6c' }
// 未知/空           → { label:'通知', icon:'Bell', color:'#909399' }
```

`MainLayout.vue` 铃铛条目标题行：图标前置（`<el-icon><component :is="meta.icon"/></el-icon>`）+ 着色 dot（替换现有灰色 `.dot`）。保留 unread 高亮与「全部已读」。后端 `_notif_dto` 已返回 `event_type`，前端直接取用。

## 3. 边界条件

- 电影 `movie:` 前缀的 episode 键由 `media.media_type` 判定兜底；即使 episode 键异常，`is_movie` 来自 media_type 更可靠。
- 未知 event_type（旧数据）→ 前端回退灰色默认样式，不阻断渲染。
- PushPlus 未配置 token → 通道跳过，站内不受影响（现有逻辑不变）。
- 历史通知（已存在 notifications 表数据）不迁移、不改文案。

## 4. 测试策略

- 新增 `backend/tests/test_notify_templates.py`：工厂各函数文案断言（剧集/电影/集号边界 3 位集号、reason/阈值格式化）、`text_to_html` 转换断言（标题加粗、分段、高亮）。
- 更新受影响断言：
  - `test_transfer.py:281`「下载完成: ep.mkv」→ 该通知已移除，断言改为无 download_complete 事件（或删除断言）
  - `test_transfer.py:531`「下载开始: …」→ 断言无 download_started 事件
  - `test_library_check.py:302`「入库完成」标题含 media_id → 改断言新文案格式（媒体名 · SxxExx，不含 media_id/file_name）
  - `test_capacity_alert.py:96`「夸克容量使用率过高」→ 若有改动按新文案更新
  - `test_oracle_fixes.py` wr/tq 文案断言 → 按新格式更新（前缀保留）
  - `test_nastools_notify.py` 相关断言 → 中文事件名
  - `test_admin_users.py:292`（测试数据构造，如受影响则更新）
- 前端：`npm run build`（或项目前端构建命令）验证。

## 5. 迁移与回滚

- 纯代码变更，无数据库迁移。
- 回滚：git revert 即可，通知表历史数据不受影响。
- 部署顺序无强依赖（后端通知与前端渲染独立，前端对未知类型有回退）。

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 移除通知破坏测试断言较多 | 已列出受影响测试，随任务同步更新 |
| 去重前缀被工厂重构破坏 | 前缀拼接留在调用方，工厂纯文案；专项断言 de-dup 用例 |
| PushPlus HTML 兼容性 | 只用 b/p/span 简单标签；txt 兜底逻辑保留 |
| 前端图标组件名拼写错误 | 用 element-plus 稳定图标名，build 验证 |

## 7. 待办（与 tasks.md 对应）

后端文案工厂 → 移除 3 处通知 → 入库完成文案 → 其余通知点接入 → PushPlus HTML → 前端铃铛 → 集成验证。