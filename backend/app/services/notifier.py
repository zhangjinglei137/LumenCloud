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
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from app.config import settings
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

    settings.PUSHPLUS_TOKEN 为空时 notifier 跳过本通道（enabled=False）。
    推送实现在 pushplus 服务（后续阶段），失败降级站内不影响主流程。
    """

    def __init__(self) -> None:
        self._client: Optional[PushPlusClient] = None
        token = (settings.PUSHPLUS_TOKEN or "").strip()
        if token:
            self._client = PushPlusClient(token=token)

    async def notify(self, event: NotifyEvent) -> None:
        if self._client is None:
            return  # 未配置 PushPlus，跳过
        try:
            await self._client.send(title=event.title, content=event.body)
        except Exception:
            logger.exception("PushPlus 推送失败（降级站内，event=%s）", event.event_type)


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
