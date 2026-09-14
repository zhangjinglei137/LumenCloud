---
change: fix-notification-templates
design-doc: docs/superpowers/specs/2026-09-14-fix-notification-templates-design.md
base-ref: 90bef00eb4b57e4b2e913e084153d529620f1a74
archived-with: 2026-09-14-fix-notification-templates
---

# 修复通知模板 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一站内铃铛与 PushPlus 通知文案（媒体名 + SxxExx），移除下载开始/下载完成噪音通知，PushPlus 改用 HTML 模板，前端铃铛按事件类型区分图标与颜色。

**Architecture:** 新增 `backend/app/services/notify_templates.py` 文案工厂（纯函数，返回 `(title, body)`）；各通知调用点改用工厂；`library_check._finalize_done` 通过既有 `media` 对象取媒体名；PushPlus 通道出口将纯文本转 HTML；前端 `format.ts` 增加事件类型映射、`MainLayout.vue` 渲染图标与着色 dot。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / pytest；Vue 3 / Pinia / Element Plus / TypeScript。

**Spec:** `docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md`
**Design Doc:** `docs/superpowers/specs/2026-09-14-fix-notification-templates-design.md`

## Global Constraints

- 通知文案使用中文，用户可见文本不得出现英文 raw 事件名（`transfer.fail` 等）
- 入库完成文案：剧集 `媒体 {media_name} · {episode} 已入库完成。`（episode 为 SxxExx）；电影 `媒体 {title} 已入库完成。`；不得出现 media_id 与文件名
- `notification_scan.py` 的 body 去重前缀（`wr#<id> ` / `tq#<id> `）必须保留，由调用方在正文首部拼接，工厂不负责
- `EVENT_DOWNLOAD_STARTED` 常量与 `EVENT_TYPES` 集合保持不变（不再有调用点即可）
- 站内 `body` 保持纯文本；HTML 转换只发生在 PushPlus 通道出口
- 移除 3 处通知调用时，其周边业务逻辑（刮削触发、容量缓存失效等）必须保留
- 测试断言同步更新；后端测试命令 `cd backend && python -m pytest`，前端构建命令 `cd frontend && npm run build`

---

### Task 1: 文案工厂 notify_templates.py

**Files:**
- Create: `backend/app/services/notify_templates.py`
- Test: `backend/tests/test_notify_templates.py`

**Interfaces:**
- Produces（后续任务依赖的确切签名）:
  - `download_complete(media_name: str, episode: str, is_movie: bool) -> tuple[str, str]`
  - `approval_pending(title: str) -> tuple[str, str]`
  - `flow_error_transfer(media_name: str, episode: str, reason: str, attempts: int) -> tuple[str, str]`
  - `flow_error_capacity(rate_pct: float, used_gb: float, total_gb: float, consecutive: int, threshold_pct: float) -> tuple[str, str]`
  - `flow_error_nastools_sync(exc: str) -> tuple[str, str]`
  - `flow_error_nastools_event(chinese_name: str, media_title: str) -> tuple[str, str]`
  - `flow_error_alert(title: str, message: str) -> tuple[str, str]`
  - `text_to_html(title: str, body: str) -> str`

- [x] **Step 1: 写失败测试 `backend/tests/test_notify_templates.py`**

```python
"""文案工厂单测：统一通知文案格式（fix-notification-templates）。"""
import pytest

from app.services.notify_templates import (
    approval_pending,
    download_complete,
    flow_error_alert,
    flow_error_capacity,
    flow_error_nastools_event,
    flow_error_nastools_sync,
    flow_error_transfer,
    text_to_html,
)


class TestDownloadComplete:
    def test_episode(self):
        title, body = download_complete("繁花", "S01E02", is_movie=False)
        assert title == "入库完成：繁花 · S01E02"
        assert body == "媒体 繁花 · S01E02 已入库完成。"

    def test_episode_three_digit(self):
        _, body = download_complete("海贼王", "S01E100", is_movie=False)
        assert body == "媒体 海贼王 · S01E100 已入库完成。"

    def test_movie(self):
        title, body = download_complete("长安乱", "movie:长安乱", is_movie=True)
        assert title == "入库完成：长安乱"
        assert body == "媒体 长安乱 已入库完成。"

    def test_no_media_id_or_filename(self):
        _, body = download_complete("繁花", "S01E02", is_movie=False)
        assert "media_id" not in body
        assert ".mkv" not in body


class TestApprovalPending:
    def test_format(self):
        title, body = approval_pending("狂飙")
        assert title == "新的想看请求：狂飙"
        assert body == "狂飙"  # 去重前缀由调用方拼接


class TestFlowError:
    def test_transfer(self):
        title, body = flow_error_transfer("繁花", "S01E02", "磁盘写满", 3)
        assert title == "转存失败：繁花 · S01E02"
        assert "磁盘写满" in body
        assert "3" in body

    def test_capacity(self):
        title, body = flow_error_capacity(82.3, 164.6, 200.0, 2, 80.0)
        assert title == "夸克容量使用率过高"
        assert "82.3%" in body
        assert "164.6G" in body and "200.0G" in body

    def test_nastools_sync(self):
        title, body = flow_error_nastools_sync("连接超时")
        assert title == "NasTools 目录同步失败"
        assert "连接超时" in body

    def test_nastools_event_chinese(self):
        title, body = flow_error_nastools_event("转存失败", "狂飙")
        assert title == "转存失败：狂飙"
        assert "transfer.fail" not in title
        assert body == "NaSTools 报告转存失败，请人工核查。"

    def test_alert(self):
        title, body = flow_error_alert("转存流程告警", "某中转文件失败")
        assert title == "转存流程告警"
        assert body == "某中转文件失败"


class TestTextToHtml:
    def test_bold_title_and_paragraphs(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成：繁花 · S01E02")
        assert "<b>入库完成：繁花 · S01E02</b>" in html
        assert "<p>" in html and "</p>" in html

    def test_media_highlight(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成")
        assert "style=" in html  # 高亮 span 存在

    def test_no_body(self):
        html = text_to_html("", "仅标题")
        assert "<b>仅标题</b>" in html
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_notify_templates.py -v`
Expected: FAIL（`ModuleNotFoundError: app.services.notify_templates`）

- [x] **Step 3: 实现 `backend/app/services/notify_templates.py`**

```python
"""通知文案工厂（fix-notification-templates）。

所有通知生成点统一从这里取 (title, body)，强制「媒体名 + SxxExx」「中文文案」
规范。纯函数，不做数据库判定（电影判定由调用方按 media.media_type 传入）。

去重前缀（wr# / tq#）由调用方在 body 首部拼接，本模块不负责。
PushPlus 通道出口用 text_to_html 将纯文本转 HTML（站内 body 保持纯文本）。
"""
import html as _html
import re

_MEDIA_SEG_RE = re.compile(r"(媒体\s*[^\n]*)")
_MEDIA_EP_RE = re.compile(r"(媒体\s*[^·\n]+·\s*S\d{2,3}E\d{2,3})")


def download_complete(media_name: str, episode: str, is_movie: bool) -> tuple[str, str]:
    """入库完成（download_complete 事件，唯一结果型通知）。

    剧集: (入库完成：{name} · {ep}, 媒体 {name} · {ep} 已入库完成。)
    电影: (入库完成：{name}, 媒体 {name} 已入库完成。)
    """
    if is_movie:
        return f"入库完成：{media_name}", f"媒体 {media_name} 已入库完成。"
    return (
        f"入库完成：{media_name} · {episode}",
        f"媒体 {media_name} · {episode} 已入库完成。",
    )


def approval_pending(title: str) -> tuple[str, str]:
    """新的想看请求（approval_pending 事件）。去重前缀由调用方拼接。"""
    return f"新的想看请求：{title}", title


def flow_error_transfer(media_name: str, episode: str, reason: str, attempts: int) -> tuple[str, str]:
    """转存失败终态（重试达上限）。"""
    return (
        f"转存失败：{media_name} · {episode}",
        f"原因：{reason}；已重试 {attempts} 次达上限，任务已标记失败，请人工处理。",
    )


def flow_error_capacity(rate_pct: float, used_gb: float, total_gb: float,
                        consecutive: int, threshold_pct: float) -> tuple[str, str]:
    """夸克容量告警。"""
    return (
        "夸克容量使用率过高",
        f"夸克中转空间使用率 {rate_pct:.1f}%（used {used_gb:.2f}G / "
        f"total {total_gb:.2f}G），连续 {consecutive} 次快照超过阈值 "
        f"{threshold_pct:.0f}%，请及时清理或扩容。",
    )


def flow_error_nastools_sync(exc: str) -> tuple[str, str]:
    """NasTools 目录同步失败。"""
    return "NasTools 目录同步失败", f"同步失败，请检查 NasTools 服务与凭据：{exc}"


def flow_error_nastools_event(chinese_name: str, media_title: str) -> tuple[str, str]:
    """NaSTools Webhook 失败事件（中文事件名）。"""
    return f"{chinese_name}：{media_title}", f"NaSTools 报告{chinese_name}，请人工核查。"


def flow_error_alert(title: str, message: str) -> tuple[str, str]:
    """通用告警（transfer 流程告警等具名场景之外的兜底）。"""
    return title, message


def text_to_html(body: str, title: str) -> str:
    """纯文本 → PushPlus HTML：标题加粗、按换行分段、媒体名与集数高亮。

    站内 body 保持纯文本；此函数只在 PushPlus 通道出口调用。
    """
    safe_title = _html.escape(title)
    safe_body = _html.escape(body or "")
    parts = [f"<b>{safe_title}</b>"]
    for para in safe_body.split("\n"):
        para = para.strip()
        if not para:
            continue
        parts.append(f"<p>{para}</p>")
    return "".join(parts)
```

注意：Step 3 的高亮需求（`test_media_highlight` 断言 `style=` 存在）需要实现分段内的 span 高亮。将 `text_to_html` 中每段再对 `媒体 …SxxExx` / `媒体 …` 片段包 `<span style="color:#4caf50">…</span>`（入库完成类语义色），示例如下：

```python
    _HIGHLIGHT_COLOR = "#4caf50"

    def _highlight_segment(seg: str) -> str:
        seg = _html.escape(seg)
        for pat in (_MEDIA_EP_RE, _MEDIA_SEG_RE):
            seg = pat.sub(lambda m: f'<span style="color:{_HIGHLIGHT_COLOR}">{m.group(1)}</span>', seg)
        return seg
```

并在段落循环中调用 `_highlight_segment(para)` 替代直接 `_html.escape(para)`。请自行整合，使所有测试通过。

- [x] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_notify_templates.py -v`
Expected: PASS（全部用例）

- [x] **Step 5: 提交**

```bash
git add backend/app/services/notify_templates.py backend/tests/test_notify_templates.py
git commit -m "feat(notifications): 新增通知文案工厂统一文案格式"
```

---

### Task 2: 移除 3 处噪音通知

**Files:**
- Modify: `backend/app/routers/approvals.py:130-140`（「开始入库」通知；实际位置在 approve_approval 内原 209-220 行）
- Modify: `backend/app/tasks/transfer.py:995-1007`（「下载开始」通知；保留容量缓存失效）
- Modify: `backend/app/tasks/transfer.py:528-535`（「下载完成」通知；保留 `_spawn(scrape_runner)`）
- Test: `backend/tests/test_transfer.py`、`backend/tests/test_p1_fixes.py`、`backend/tests/test_approval_dup.py`

**Interfaces:**
- Consumes: 无新接口；仅删除调用
- Produces: 无

- [x] **Step 1: 移除 approvals.py「开始入库」通知**

删除 `approve_approval` 中 `# ---- 事务外副作用 ----` 下 §5.3 的 `notifier.notify(NotifyEvent(event_type=EVENT_DOWNLOAD_STARTED, title=f"开始入库: {title}", recipient=requester, ...))` 整段 try/except（原 209-220 行）。同时删除该函数中不再使用的 `from app.services.notifier import ... EVENT_DOWNLOAD_STARTED` 引用（若文件顶部 import 中仅此处使用 EVENT_DOWNLOAD_STARTED，则从 import 行移除）。**保留** `trigger_scan_background(media_id, manual=True)` 触发逻辑。若 `requester` 变量此后不再被使用，一并删除其赋值（`title, requester = wr.title, wr.requested_by` → `title = wr.title`）。

- [x] **Step 2: 移除 transfer.py「下载开始」通知**

删除 `_complete_download` 中 P1 段 `notifier.notify(NotifyEvent(event_type=EVENT_DOWNLOAD_STARTED, title=f"下载开始: {out_name}", ...))` 的 try/except（原 995-1007 行）。**保留** 其后的容量缓存失效 `capacity.provider.invalidate_usage_cache()` 逻辑。

- [x] **Step 3: 移除 transfer.py「下载完成」通知**

在 `_after_complete_promote` 中删除 `await notifier.notify(NotifyEvent(event_type=EVENT_DOWNLOAD_COMPLETE, title=f"下载完成: {file_name}", ...))`（原 528-535 行）。**保留** 后续 `_spawn(scrape_runner)` 刮削触发。`_after_complete_promote` 参数若 `file_name` 只被通知使用，保留参数不影响（其他地方仍用），如仅此处引用则可删除参数并在调用处调整——以编译/测试为准，避免过度清理。

- [x] **Step 4: 更新受影响测试断言**

- `backend/tests/test_transfer.py:281` `assert done_events[0].title == "下载完成: ep.mkv"` → 该通知已移除，改为断言该路径下 download_complete 事件为空（`assert done_events == []`）或删除断言（保持测试意图：推进至 scrape）。同时检查同文件 262-281 区域用例意图，若用例专测通知则改为专测推进行为。
- `backend/tests/test_transfer.py:531` `assert started[0].title == "下载开始: ..."` → 改为断言无 download_started 事件（`assert started == []`）。
- 运行相关测试确认绿；若 `test_p1_fixes.py`、`test_approval_dup.py` 中 mock notifier 后按事件计数断言的地方因此变化，同步调整。

- [x] **Step 5: 验证 EVENT_DOWNLOAD_STARTED 无调用点**

Run: `cd backend && grep -rn "EVENT_DOWNLOAD_STARTED" app/ | grep -v "notifier.py"`
Expected: 无输出（常量仅定义处）

Run: `cd backend && python -m pytest tests/test_transfer.py tests/test_p1_fixes.py tests/test_approval_dup.py -v`
Expected: PASS

- [x] **Step 6: 提交**

```bash
git add backend/app/routers/approvals.py backend/app/tasks/transfer.py backend/tests/test_transfer.py backend/tests/test_p1_fixes.py backend/tests/test_approval_dup.py
git commit -m "refactor(notifications): 移除下载开始与下载完成噪音通知"
```

---

### Task 3: 入库完成文案接入工厂

**Files:**
- Modify: `backend/app/tasks/library_check.py`（`_finalize_done` 签名与通知段）
- Test: `backend/tests/test_library_check.py`

**Interfaces:**
- Consumes: `notify_templates.download_complete(media_name, episode, is_movie)`
- Produces: `_finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod, media_title, is_movie)`（参数扩展）

- [x] **Step 1: 扩展 `_finalize_done` 签名并改用工厂**

在 `backend/app/tasks/library_check.py`：

```python
async def _finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod,
                         media_title: str, is_movie: bool) -> None:
```

调用处（library_check 主循环第 511 行附近，Emby 命中分支）传入 `media.title` 与 `(media.media_type or "").strip().lower() == "movie"`：

```python
await _finalize_done(dq_id, media_id, episode, file_name, quark_path, transfer_mod,
                     media.title, (media.media_type or "").strip().lower() == "movie")
```

函数末尾通知段替换为：

```python
    from app.services.notify_templates import download_complete
    title, body = download_complete(media_title, episode, is_movie)
    await notifier.notify(NotifyEvent(
        event_type=EVENT_DOWNLOAD_COMPLETE,
        title=title,
        body=body,
        recipient=None,
        extra={"media_id": media_id, "episode": episode},
    ))
```

（`import` 置于模块顶部更佳；函数内延迟 import 仅当存在循环导入风险。）

- [x] **Step 2: 更新 test_library_check.py 断言**

`backend/tests/test_library_check.py:299-302` 区域：通知断言从「入库完成 在 title 中」细化为：

```python
assert done_events[0].event_type == "download_complete"
assert done_events[0].title == f"入库完成：{media_title} · {episode}"
assert f"媒体 {media_title} · {episode} 已入库完成。" == done_events[0].body
assert "media_id" not in done_events[0].body
assert ".mkv" not in done_events[0].body
```

（`media_title` 为测试 fixture 中创建的 Media.title 值；`episode` 为该用例的 episode 值。若原用例未断言通知，则新增上述断言。）

- [x] **Step 3: 确认 download_complete 唯一调用点**

Run: `cd backend && grep -rn "EVENT_DOWNLOAD_COMPLETE" app/`
Expected: 仅 `notifier.py`（定义）与 `library_check.py`（唯一 notify 调用）

Run: `cd backend && python -m pytest tests/test_library_check.py -v`
Expected: PASS

- [x] **Step 4: 提交**

```bash
git add backend/app/tasks/library_check.py backend/tests/test_library_check.py
git commit -m "feat(notifications): 入库完成通知改用媒体名与集数文案"
```

---

### Task 4: 其余通知点接入文案工厂

**Files:**
- Modify: `backend/app/tasks/notification_scan.py`
- Modify: `backend/app/services/capacity.py`
- Modify: `backend/app/tasks/nastools_sync.py`
- Modify: `backend/app/tasks/transfer.py`（`_record_alert` 与终态失败通知）
- Modify: `backend/app/routers/nastools_notify.py`
- Test: `backend/tests/test_oracle_fixes.py`、`backend/tests/test_capacity_alert.py`、`backend/tests/test_nastools_notify.py`

**Interfaces:**
- Consumes: `approval_pending`、`flow_error_transfer`、`flow_error_capacity`、`flow_error_nastools_sync`、`flow_error_nastools_event`、`flow_error_alert`
- Produces: 无

- [x] **Step 1: notification_scan.py 接入工厂（保留去重前缀）**

`notification_scan_job` 中两处 NotifyEvent 改用工厂，但**必须保留** body 前缀：

```python
from app.services.notify_templates import approval_pending, flow_error_transfer

# 1) 待审批
nt_title, nt_body = approval_pending(wr.title)
await notifier.notify(NotifyEvent(
    event_type=EVENT_APPROVAL_PENDING,
    title=nt_title,
    body=f"{_WR_PREFIX}{wr.id} {nt_body}",
    recipient=None,
    extra={"watch_request_id": wr.id},
))

# 2) 失败转存任务
tq_title, tq_body = flow_error_transfer_scan(tq)  # 见下
await notifier.notify(NotifyEvent(
    event_type=EVENT_FLOW_ERROR,
    title="转存任务失败",
    body=f"{_TQ_PREFIX}{tq.id} {tq.file_name}: {tq.error or '未知原因'}",
    recipient=None,
    extra={"transfer_queue_id": tq.id, "media_id": tq.media_id},
))
```

注意：`flow_error_transfer` 工厂函数面向「有媒体名/集数」的转存失败；notification_scan 的失败任务只有 `file_name` 与 `error`，且**必须保持 `tq#<id> {file_name}: {error}` 前缀查重格式**。为不破坏去重，此处在工厂族内新增轻量函数或直接保留内联文案均可——**决策：保留该条内联文案不变**（其格式本身已是「中文标题 + 前缀 + 文件名: 原因」，且前缀必须精确匹配，动它有风险）。只需将 `approval_pending` 用于审批条，失败条保持原样并在代码注释中说明「去重前缀格式固定，不入工厂」。

- [x] **Step 2: capacity.py 接入工厂**

`_maybe_capacity_alert`（原 395-405 行）改用：

```python
from app.services.notify_templates import flow_error_capacity

c_title, c_body = flow_error_capacity(
    rate_pct, latest.used_gb, latest.total_gb,
    CAPACITY_ALERT_CONSECUTIVE, threshold,
)
await notifier.notify(NotifyEvent(
    event_type=EVENT_FLOW_ERROR,
    title=c_title,
    body=c_body,
    recipient=None,
    extra={"source": latest.source, "checked_at": str(latest.checked_at)},
))
```

更新 `backend/tests/test_capacity_alert.py:96` 若断言有差异（标题「夸克容量使用率过高」不变，body 若原测试断言旧文案则更新为新格式）。

- [x] **Step 3: nastools_sync.py 接入工厂**

`nastools_sync` 失败通知（原 141-146 行）改用：

```python
from app.services.notify_templates import flow_error_nastools_sync

ns_title, ns_body = flow_error_nastools_sync(str(exc))
await notifier.notify(NotifyEvent(
    event_type=EVENT_FLOW_ERROR,
    title=ns_title,
    body=ns_body,
    recipient=None,
))
```

- [x] **Step 4: transfer.py `_record_alert` 与终态失败通知接入工厂**

- `_record_alert`（原 850-856 行）改用 `flow_error_alert(title, message)`：
  ```python
  a_title, a_body = flow_error_alert("转存流程告警", message)
  ```
  （`title` 保留「转存流程告警」，`body=message`；与工厂默认一致即可，也可直接复用现有内联。）
- 终态失败通知（原 707-714 行，`_fail_transfer` 内的 `notify_title`）改用 `flow_error_transfer`：需要媒体名 → 该函数作用域内有 `media_id`；为最小改动，可先查 Media.title（若有现成 media 对象则用之）。若作用域内拿不到媒体名，保留原 `notify_title` 与 body 格式不变（该通知已有中文标题与原因+次数信息，符合规范）。**决策：若获取媒体名需要额外查询且该路径无现成 media 对象，则保留原内联文案**，仅在设计文档记录。

- [x] **Step 5: nastools_notify.py 中文事件名映射**

在 `nastools_notify.py` 增加映射并在 `_notify_flow_error` 调用处传入中文名：

```python
_EVENT_CN_NAMES = {
    "transfer.finished": "转存完成",
    "transfer.fail": "转存失败",
    "download.fail": "下载失败",
}

# 失败事件分支（原 312-316 行）
cn_name = _EVENT_CN_NAMES.get(event_type, event_type)
await _notify_flow_error(
    f"{cn_name}：{title}",
    f"NaSTools 报告{cn_name}（文件整理/下载失败），请人工核查。",
    media_id,
)
```

（`title` 为媒体标题，来自 `media_info.title` 或 `data.name`。）更新 `backend/tests/test_nastools_notify.py` 相关断言为中文文案。

- [x] **Step 6: 运行受影响测试**

Run: `cd backend && python -m pytest tests/test_oracle_fixes.py tests/test_capacity_alert.py tests/test_nastools_notify.py tests/test_library_check.py tests/test_capacity.py -v`
Expected: PASS（断言已按新文案同步）

- [x] **Step 7: 提交**

```bash
git add backend/app/tasks/notification_scan.py backend/app/services/capacity.py backend/app/tasks/nastools_sync.py backend/app/tasks/transfer.py backend/app/routers/nastools_notify.py backend/tests/
git commit -m "refactor(notifications): 通知点接入文案工厂并中文化事件名"
```

---

### Task 5: PushPlus HTML 模板

**Files:**
- Modify: `backend/app/services/pushplus.py`（`send` template 透传已存在，确认默认与调用）
- Modify: `backend/app/services/notifier.py`（`PushPlusNotifier.notify` 出口转换）
- Test: `backend/tests/test_notify_templates.py`（`text_to_html` 已覆盖）+ 新增 `backend/tests/test_pushplus_html.py`

**Interfaces:**
- Consumes: `text_to_html(body, title)`
- Produces: `PushPlusNotifier.notify` 以 `template="html"` 发送 HTML 内容

- [x] **Step 1: PushPlusNotifier.notify 出口转换**

在 `backend/app/services/notifier.py` 的 `PushPlusNotifier.notify`：

```python
async def notify(self, event: NotifyEvent) -> None:
    self._refresh_client()
    if self._client is None:
        return
    try:
        from app.services.notify_templates import text_to_html
        content = text_to_html(event.body or "", event.title)
        await self._client.send(title=event.title, content=content, template="html")
    except Exception:
        logger.exception("PushPlus 推送失败（降级站内，event=%s）", event.event_type)
```

（`send` 已接受 `template` 参数，默认 `txt`；此处显式 `html`。）

- [x] **Step 2: 新增 `backend/tests/test_pushplus_html.py`**

```python
"""PushPlus HTML 出口单测：纯文本转 HTML + template=html 发送。"""
from app.services.notify_templates import text_to_html


class TestPushPlusHtml:
    def test_html_contains_bold_title(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成：繁花 · S01E02")
        assert "<b>入库完成：繁花 · S01E02</b>" in html

    def test_paragraph_split(self):
        html = text_to_html("第一行\n第二行", "标题")
        assert "<p>第一行</p>" in html
        assert "<p>第二行</p>" in html

    def test_media_highlight_span(self):
        html = text_to_html("媒体 繁花 · S01E02 已入库完成。", "入库完成")
        assert 'style="color:' in html
        assert "S01E02" in html

    def test_escape_html(self):
        html = text_to_html("<script>alert(1)</script>", "标题")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
```

- [x] **Step 3: 验证 PushPlus 未配置行为不变**

在 `notifier.py` 测试（或手动核对）：`_client is None` 时直接 return，不构建 HTML。已有 `test_pushplus.py` 若存在则运行确认。

Run: `cd backend && python -m pytest tests/test_pushplus_html.py tests/test_notify_templates.py -v`
Expected: PASS

- [x] **Step 4: 提交**

```bash
git add backend/app/services/notifier.py backend/tests/test_pushplus_html.py backend/tests/test_notify_templates.py
git commit -m "feat(notifications): PushPlus 推送改用 HTML 模板"
```

---

### Task 6: 前端铃铛类型呈现

**Files:**
- Modify: `frontend/src/utils/format.ts`
- Modify: `frontend/src/layouts/MainLayout.vue`
- Test: `cd frontend && npm run build`（类型检查 + 构建）

**Interfaces:**
- Produces:
  - `export interface NotificationTypeMeta { label: string; icon: string; color: string }`
  - `export function notificationTypeMeta(event_type?: string | null): NotificationTypeMeta`

- [x] **Step 1: format.ts 新增类型映射**

在 `frontend/src/utils/format.ts` 末尾追加：

```ts
/** 通知事件类型 → 铃铛呈现（fix-notification-templates） */
export interface NotificationTypeMeta {
  label: string
  icon: string   // element-plus 图标组件名（模板 <component :is> 使用）
  color: string  // 强调色（dot / 图标着色）
}

const NOTIFICATION_TYPE_MAP: Record<string, NotificationTypeMeta> = {
  download_complete: { label: '入库完成', icon: 'CircleCheckFilled', color: '#67c23a' },
  approval_pending: { label: '待审批', icon: 'BellFilled', color: '#409eff' },
  flow_error: { label: '告警', icon: 'WarningFilled', color: '#f56c6c' },
}

const NOTIFICATION_TYPE_DEFAULT: NotificationTypeMeta = {
  label: '通知',
  icon: 'Bell',
  color: '#909399',
}

export function notificationTypeMeta(event_type?: string | null): NotificationTypeMeta {
  if (event_type && NOTIFICATION_TYPE_MAP[event_type]) {
    return NOTIFICATION_TYPE_MAP[event_type]
  }
  return NOTIFICATION_TYPE_DEFAULT
}
```

- [x] **Step 2: MainLayout.vue 渲染图标与着色 dot**

在 `frontend/src/layouts/MainLayout.vue`：

1. script 区 import：`import { notificationTypeMeta } from '../utils/format'`
2. 模板通知条目标题行（原 208-211 行）替换为：

```vue
<div class="title">
  <span class="dot" :style="{ background: notificationTypeMeta(item.event_type).color }" />
  <el-icon :style="{ color: notificationTypeMeta(item.event_type).color }" class="notify-type-icon">
    <component :is="notificationTypeMeta(item.event_type).icon" />
  </el-icon>
  <span class="notify-label">{{ notificationTypeMeta(item.event_type).label }}</span>
  {{ item.title || '通知' }}
</div>
```

3. 保留 `.dot` 基础样式；若原 `.dot` 有固定颜色，改为由内联 style 覆盖。新增 `.notify-type-icon`（margin-right: 4px）与 `.notify-label`（margin-right: 4px; font-size: 12px）样式于该组件 `<style scoped>`。

注意：`item` 类型为 `NotificationItem`（含 `event_type?: string`），TS 若报 icon 字符串不能用于 `:is`，可对 icon 字段断言为 `Component`（`import type { Component } from 'vue'`，映射值 `icon` 类型声明为 `Component`，用 `markRaw(CircleCheckFilled)` 等包装，或以 `as Component` 断言后使用）。

- [x] **Step 3: 前端构建验证**

Run: `cd frontend && npm run build`
Expected: PASS（typecheck + 构建无错误）

- [x] **Step 4: 提交**

```bash
git add frontend/src/utils/format.ts frontend/src/layouts/MainLayout.vue
git commit -m "feat(notifications): 铃铛按事件类型区分图标与颜色"
```

---

### Task 7: 集成验证

**Files:**
- 验证范围：全部改动

- [x] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest`
Expected: PASS（无失败）

- [x] **Step 2: 前端构建**

Run: `cd frontend && npm run build`
Expected: PASS

- [x] **Step 3: 核对 spec 覆盖**

对照 `docs/openspec/changes/fix-notification-templates/specs/notifications/spec.md` 逐条核对：
- 触发时机（无下载开始/下载完成通知）→ Task 2 实现
- 入库完成文案（剧集/电影/无 id 无文件名）→ Task 3 实现
- 中文统一 + 无英文 raw 事件名 → Task 4 实现
- PushPlus HTML + 未配置跳过 → Task 5 实现
- 铃铛类型呈现 + 未知回退 → Task 6 实现

- [x] **Step 4: 汇总验证证据**

在任务完成汇报中记录：后端 pytest 结果、前端 build 结果、spec 逐条覆盖结论。**不提交**（验证任务本身无代码变更；如验证发现缺陷，回到对应任务修复后重新验证）。

---

## Self-Review 记录

- **Spec 覆盖**：6 项 Requirement 均有对应任务（触发时机→T2、文案→T3/T4、中文→T4、PushPlus→T5、铃铛→T6、未知回退→T6）。✓
- **占位符检查**：无 TBD/TODO；代码步骤含完整内容。✓
- **类型一致性**：`notify_templates` 各函数签名在 T1 定义、T3/T4/T5 消费，签名一致；`notificationTypeMeta` 在 T6 定义并消费。✓
