"""
PushPlus 推送客户端（docs/新系统设计.md §7 / S1 修复）。

契约（S1 修复）：
    POST https://www.pushplus.plus/send
    参数走 **form body**（token/title/content/template），不再 GET+query 传长 content。
    settings.PUSHPLUS_TOKEN 为空时视为未启用（notifier 层整体跳过 PushPlus 通道）。
    响应 JSON 含 code 字段，code != 200 → 业务失败（抛 PushPlusUnavailable 带 message）。
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

PUSHPLUS_SEND_ENDPOINT = "https://www.pushplus.plus/send"

REQUEST_TIMEOUT = httpx.Timeout(15.0)


class PushPlusUnavailable(Exception):
    """PushPlus 服务不可用：网络故障 / 非 2xx / 业务失败。"""


class PushPlusClient:
    """PushPlus 客户端（单条推送）。"""

    def __init__(self, token: Optional[str] = None) -> None:
        # Phase 8 配置入库：显式传入的 token 优先；否则 DB（config_store）优先、
        # env fallback（PATCH 保存后新实例即读最新值；notifier 层每次 notify 重建）
        self._token: Optional[str] = (
            token
            or (config_store.get("pushplus_token", settings.PUSHPLUS_TOKEN) or "").strip()
            or None
        )
        self._endpoint: str = PUSHPLUS_SEND_ENDPOINT

    @property
    def enabled(self) -> bool:
        """未配置 token 时视为未启用（notifier 按此跳过该通道）。"""
        return bool(self._token)

    async def send(self, title: str, content: str, template: str = "txt") -> dict[str, Any]:
        """发送一条推送。

        参数（S1 修复）：**POST form body** 传 token/title/content/template。
        返回: PushPlus 响应 JSON（code/message/data 等）。
        异常: PushPlusUnavailable —— 未配置 token / HTTPError / 非 2xx /
              JSON 异常 / code != 200（带业务 message）。
        """
        if not self._token:
            raise PushPlusUnavailable("PUSHPLUS_TOKEN 未配置，PushPlus 通道不可用")

        form = {
            "token": self._token,
            "title": title,
            "content": content,
            "template": template,
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(self._endpoint, data=form)
            except httpx.HTTPError as exc:
                logger.warning("PushPlus 推送请求失败: %s", exc)
                raise PushPlusUnavailable(f"PushPlus 推送请求失败: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("PushPlus 非 2xx: %s", resp.status_code)
            raise PushPlusUnavailable(f"PushPlus 返回 HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise PushPlusUnavailable("PushPlus 响应不是合法 JSON") from exc

        code = payload.get("code")
        if code != 200:
            message = payload.get("message") or payload.get("msg") or payload.get("data")
            logger.warning("PushPlus 业务失败: code=%s %s", code, message)
            raise PushPlusUnavailable(f"PushPlus 业务失败: {message}")

        return payload


# 模块级单例
client = PushPlusClient()