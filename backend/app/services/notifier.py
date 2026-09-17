"""
通知抽象（docs/新系统设计.md §7）。

- Notifier Protocol  : notify(event: NotifyEvent) 抽象
- InAppNotifier      : 站内消息，写 notifications 表（始终启用，前端铃铛）
- PushPlusNotifier   : PushPlus 通道（可选；settings.PUSHPLUS_TOKEN 为空则整体跳过）
- NotifierChain      : 链式分发，单通道异常不阻断其他通道与主流程

事件表（全局开关在 system_config，默认均开）：
    download_complete / download_started / flow_error / approval_pending
空跑 / 无遗漏 不推送（消灭 P1 噪音）。
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from app.config import settings
from app.services import config_store
from app.services.pushplus import PushPlusClient

logger = logging.getLogger(__name__)

# 事件类型常量（与 notifications.event_type 存储值一致）
EVENT_DOWNLOAD_COMPLETE = "download_complete"
EVENT_DOWNLOAD_STARTED = "download_started"
EVENT_FLOW_ERROR = "flow_error"
EVENT_APPROVAL_PENDING = "approval_pending"

EVENT_TYPES = frozenset({
    EVENT_DOWNLOAD_COMPLETE,
    EVENT_DOWNLOAD_STARTED,
    EVENT_FLOW_ERROR,
    EVENT_APPROVAL_PENDING,
})

# PushPlus 失败降级节流窗（秒）：通道持续故障（无 media 维度、无自然冷却）时
# 每次推送都会失败，10 分钟内只向站内补发一次「推送失败」告警，防刷屏
# （对齐 transfer._alert_cooldown 模式：模块级 dict + TTL 清理 + 指纹）。
_PUSHPLUS_ALERT_COOLDOWN_SECONDS = 600.0
# PushPlus 失败降级节流表。key = "pushplus"（全局通道，无 media_id 维度）；
# 值 = (最近补发的 monotonic 时间戳, 指纹)。窗口内同指纹重复失败 → 跳过补发。
_pushplus_alert_cooldown: dict[str, tuple[float, str]] = {}


@dataclass
class NotifyEvent:
    """一次通知事件。

    参数:
        event_type: 事件类型（上述 EVENT_* 常量）
        title:      标题（站内/推送共用）
        body:       正文（可为空）
        recipient:  目标用户 id；None 表示全体（notifications.recipient 为空）
        extra:      扩展字段（如 media_id / episode，供前端跳转）
    """
    event_type: str
    title: str
    body: str = ""
    recipient: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Notifier(Protocol):
    """通知器抽象。"""

    async def notify(self, event: NotifyEvent) -> None: ...


class InAppNotifier:
    """站内消息通知器（写 notifications 表，始终启用）。

    SQLAlchemy async session 通过 session_factory 注入（默认 app.database.async_session）；
    Notification 模型由并行 lane 建立（app.models），此处运行时导入，
    集成验证阶段统一确认。
    """

    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is None:
            from app.database import async_session

            session_factory = async_session
        self._session_factory = session_factory

    async def notify(self, event: NotifyEvent) -> None:
        from app.models import Notification  # noqa: F401  运行时导入（模型并行 lane 建立）

        try:
            async with self._session_factory() as session:
                session.add(
                    Notification(
                        recipient=event.recipient,
                        event_type=event.event_type,
                        title=event.title,
                        body=event.body,
                    )
                )
                await session.commit()
        except Exception:
            # 站内写库失败不影响主流程；记录日志供排查（后续阶段接入 flow_error 自通知）
            logger.exception("InAppNotifier 写库失败，通知丢失（event=%s）", event.event_type)


class PushPlusNotifier:
    """PushPlus 通知器（可选通道）。

    config_store.pushplus_token（DB 优先、env fallback）为空时 notifier 跳过本通道
    （enabled=False）。Phase 8 配置入库：每次 notify 时重建 client 读取最新 token，
    PATCH 保存即生效，无需重启。推送失败降级站内不影响主流程。
    """

    def __init__(self) -> None:
        self._client: Optional[PushPlusClient] = None
        # 失败降级目标（惰性创建 InAppNotifier；测试可注入桩验证行为）
        self._fallback: Optional[InAppNotifier] = None

    def _refresh_client(self) -> None:
        token = (config_store.get("pushplus_token", settings.PUSHPLUS_TOKEN) or "").strip()
        self._client = PushPlusClient(token=token) if token else None

    async def notify(self, event: NotifyEvent) -> None:
        self._refresh_client()
        if self._client is None:
            return  # 未配置 PushPlus，跳过
        try:
            from app.services.notify_templates import text_to_html

            # 出口转换：纯文本 body + 标题 → 加粗/分段/高亮的 HTML，显式 template=html
            content = text_to_html(event.body or "", event.title)
            await self._client.send(title=event.title, content=content, template="html")
        except Exception as exc:  # noqa: BLE001  通道失败 → 降级站内告警（审查 D7）
            logger.exception("PushPlus 推送失败（降级站内，event=%s）", event.event_type)
            await self._notify_fallback(event, exc)

    async def _notify_fallback(self, event: NotifyEvent, exc: Exception) -> None:
        """推送失败降级：向站内补发一条 flow_error 告警（节流防刷屏，防递归）。

        防递归：直接调 InAppNotifier.notify（写 notifications 表），**不经过
        NotifierChain / PushPlusNotifier**——降级通知绝不重推 PushPlus 通道。
        节流：对齐 transfer._alert_cooldown 模式（模块级 dict + TTL 清理 +
        指纹 bucket）；PushPlus 为全局通道（无 media_id 维度），固定
        key="pushplus"、指纹固定为 "pushplus_failed"，窗口内重复失败只补发一次。
        """
        now = time.monotonic()
        # TTL 清理（停留超 2 倍窗口的条目不可能再命中，遍历删除防无界增长）
        for _key, (_ts, _b) in list(_pushplus_alert_cooldown.items()):
            if now - _ts > 2 * _PUSHPLUS_ALERT_COOLDOWN_SECONDS:
                _pushplus_alert_cooldown.pop(_key, None)
        key = "pushplus"
        bucket = "pushplus_failed"
        last_ts, last_bucket = _pushplus_alert_cooldown.get(key, (0.0, None))
        if last_bucket == bucket and (now - last_ts) < _PUSHPLUS_ALERT_COOLDOWN_SECONDS:
            logger.info(
                "PushPlus 失败降级节流（%ds 内同类重复告警 %s）",
                _PUSHPLUS_ALERT_COOLDOWN_SECONDS, key,
            )
            return
        _pushplus_alert_cooldown[key] = (now, bucket)

        if self._fallback is None:
            self._fallback = InAppNotifier()
        try:
            from app.services.notify_templates import sanitize_error_text

            await self._fallback.notify(NotifyEvent(
                event_type=EVENT_FLOW_ERROR,
                title="PushPlus 推送失败",
                body=(
                    f"PushPlus 推送失败（{event.event_type}），已降级站内通知，"
                    f"请检查 PushPlus 通道配置。详情：{sanitize_error_text(str(exc))}"
                ),
                recipient=event.recipient,
            ))
        except Exception:  # noqa: BLE001  站内补发失败不影响主流程（原异常已记录日志）
            logger.exception("PushPlus 失败降级站内告警发送失败（event=%s）", event.event_type)


class NotifierChain:
    """链式通知器：依次调用各通道，单通道异常被吞掉并记日志。"""

    def __init__(self, notifiers: Optional[list[Notifier]] = None) -> None:
        self._notifiers: list[Notifier] = list(notifiers or [])

    def add(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    async def notify(self, event: NotifyEvent) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(event)
            except Exception:
                logger.exception("Notifier 通道异常（%s），跳过", type(notifier).__name__)


# 模块级单例（docs/新系统设计.md §7）
notifier = NotifierChain([InAppNotifier(), PushPlusNotifier()])
