"""占位任务：验证 APScheduler 在容器内正常运行"""
import logging

logger = logging.getLogger(__name__)


async def heartbeat() -> None:
    logger.info("[scheduler] heartbeat OK")
