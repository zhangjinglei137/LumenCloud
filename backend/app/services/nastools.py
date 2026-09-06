"""
NasTools 目录同步客户端（docs/新系统设计.md §10 / §7 N2 告警）。

契约（Q4 实证 + n8n 旧流程）：
    登录   : POST {base}/   form {next:"", username, password, remember:"on"}
             follow_redirects=False，HTTP 302 且 Set-Cookie 含 session= 视为成功
    重启   : POST {base}/do form {"cmd": "restart"}
    目录同步: POST {base}/do form {"cmd": "run_directory_sync", "sid": [...]}
             sid=None/[] 表示全部分目录

会话模型：持会话的 httpx.AsyncClient（登录后 session cookie 复用）；每次调用
自动确保已登录（_ensure_login）。login/restart/同步失败均抛 NasToolsUnavailable
（含 HTTP 状态/响应摘要），上层按 N2 触发 flow_error 告警。
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(15.0)

# 响应摘要截断长度（避免把长页面/日志写进异常信息）
_SUMMARY_LIMIT = 200


class NasToolsUnavailable(Exception):
    """NasTools 服务不可用：配置缺失 / 网络故障 / 非预期响应。"""


class NasToolsClient:
    """NasTools 客户端（登录 / 重启 / 目录同步，持会话 cookie 复用）。"""

    def __init__(self) -> None:
        # Phase 8 配置入库：base_url/username/password 改为属性每次访问经
        # config_store 读取（DB 优先、env fallback），PATCH 保存即生效。
        self._client: Optional[httpx.AsyncClient] = None
        self._session_cookie: Optional[str] = None  # 形如 "session=abc123"
        logger.debug("NasToolsClient 初始化%s", "（未配置）" if not self._base_url else "")

    @property
    def _base_url(self) -> Optional[str]:
        return (config_store.get("nastools_base_url", settings.NASTOOLS_BASE_URL) or "").strip().rstrip("/") or None

    @property
    def _username(self) -> Optional[str]:
        return config_store.get("nastools_username", settings.NASTOOLS_USERNAME) or None

    @property
    def _password(self) -> Optional[str]:
        return config_store.get("nastools_password", settings.NASTOOLS_PASSWORD) or None

    def _get_client(self) -> httpx.AsyncClient:
        """持会话的 AsyncClient（惰性创建，登录后 cookie 驻留复用）。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT,
                follow_redirects=False,
            )
        return self._client

    @staticmethod
    def _extract_session_cookie(set_cookie: str) -> Optional[str]:
        """从 Set-Cookie 头提取 "session=..." cookie（Q4 实证：302 + session= 为登录成功标志）。"""
        for part in set_cookie.split(";"):
            part = part.strip()
            if part.lower().startswith("session="):
                return part
        return None

    @staticmethod
    def _summarize(resp: httpx.Response) -> str:
        body = (resp.text or "").strip().replace("\n", " ")[:_SUMMARY_LIMIT]
        return f"HTTP {resp.status_code} {body}"

    async def login(self) -> str:
        """登录换取 session cookie（Q4 实证契约：POST {base}/ → 302 + session=）。

        返回 session cookie 字符串（如 "session=abc123"），失败抛 NasToolsUnavailable。
        """
        if not self._base_url or not self._username or not self._password:
            raise NasToolsUnavailable("NasTools 未配置（NASTOOLS_BASE_URL/USERNAME/PASSWORD）")

        form = {
            "next": "",
            "username": self._username,
            "password": self._password,
            "remember": "on",
        }
        client = self._get_client()
        try:
            resp = await client.post(f"{self._base_url}/", data=form)
        except httpx.HTTPError as exc:
            logger.warning("NasTools 登录请求失败: %s", exc)
            raise NasToolsUnavailable(f"NasTools 登录请求失败: {exc}") from exc

        cookie = self._extract_session_cookie(resp.headers.get("set-cookie", ""))
        if resp.status_code == 302 and cookie:
            self._session_cookie = cookie
            # cookie 写入 client 实例，后续请求自动携带（会话保持）
            client.cookies.set("session", cookie.split("=", 1)[1])
            logger.info("NasTools 登录成功")
            return cookie

        logger.warning("NasTools 登录未获 session（%s）", self._summarize(resp))
        raise NasToolsUnavailable(
            f"NasTools 登录失败，未获得 session cookie: {self._summarize(resp)}"
        )

    async def _ensure_login(self) -> None:
        """每次调用自动确保已登录（未持有 session 则先登录）。"""
        if not self._session_cookie:
            await self.login()

    async def _do(self, cmd: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """POST {base}/do 的公共封装（带会话 cookie，失败抛 NasToolsUnavailable）。"""
        await self._ensure_login()
        form: dict[str, Any] = {"cmd": cmd}
        if extra:
            form.update(extra)

        client = self._get_client()
        try:
            resp = await client.post(f"{self._base_url}/do", data=form)
        except httpx.HTTPError as exc:
            logger.warning("NasTools %s 请求失败: %s", cmd, exc)
            raise NasToolsUnavailable(f"NasTools {cmd} 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("NasTools %s 非 2xx: %s", cmd, self._summarize(resp))
            raise NasToolsUnavailable(
                f"NasTools {cmd} 失败: {self._summarize(resp)}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            # 2xx 但非 JSON：返回文本摘要（服务端已受理），不视为失败
            logger.info("NasTools %s 返回非 JSON 文本: %s", cmd, self._summarize(resp))
            return {"raw": (resp.text or "")[:_SUMMARY_LIMIT]}

    async def restart(self) -> dict[str, Any]:
        """重启 NasTools（绕开目录同步 bug 的前置动作，用户确认保留）。

        契约（docs §10 / n8n 原「重启插件」节点）：POST {base}/do，form {"cmd":"restart"}。
        """
        logger.info("NasTools 触发重启")
        return await self._do("restart")

    async def run_directory_sync(self, sid: Optional[list[int]] = None) -> dict[str, Any]:
        """触发目录同步（sid=None/[] 表示全部分目录）。

        契约（docs §10）：POST {base}/do，form {"cmd":"run_directory_sync", "sid": [...]}；
        调用时机：重启 + nastools_sync_cooldown_minutes=30 冷却后再调用（N1）。
        """
        extra = {"sid": list(sid)} if sid else None
        logger.info("NasTools 触发目录同步%s",
                    "（全部分目录）" if not sid else f"（sid={list(sid)}）")
        return await self._do("run_directory_sync", extra)


# 模块级单例
client = NasToolsClient()