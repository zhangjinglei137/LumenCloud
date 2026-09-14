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

# 入库完成类语义色（PushPlus HTML 高亮）
_HIGHLIGHT_COLOR = "#4caf50"


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
        f"夸克中转空间使用率 {rate_pct:.1f}%（used {used_gb:.1f}G / "
        f"total {total_gb:.1f}G），连续 {consecutive} 次快照超过阈值 "
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


def _highlight_segment(seg: str) -> str:
    """对单个段落做媒体名 / 集数高亮：先转义再依次匹配（EP 优先于 SEG，避免嵌套分组）。"""
    seg = _html.escape(seg)
    for pat in (_MEDIA_EP_RE, _MEDIA_SEG_RE):
        seg = pat.sub(
            lambda m: f'<span style="color:{_HIGHLIGHT_COLOR}">{m.group(1)}</span>', seg
        )
    return seg


def text_to_html(body: str, title: str) -> str:
    """纯文本 → PushPlus HTML：标题加粗、按换行分段、媒体名与集数高亮。

    站内 body 保持纯文本；此函数只在 PushPlus 通道出口调用。
    """
    safe_title = _html.escape(title)
    parts = [f"<b>{safe_title}</b>"]
    for para in (body or "").split("\n"):
        para = para.strip()
        if not para:
            continue
        parts.append(f"<p>{_highlight_segment(para)}</p>")
    return "".join(parts)