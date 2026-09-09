"""
aria2 JSON-RPC 客户端（docs/新系统设计.md §10 / §12.2 来源校验）。

契约（n8n 旧流程沿用）：
    JSON-RPC POST：addUri / tellStatus / getGlobalStat / tellActive
    鉴权：params 第一个元素传 aria2 token（settings.ARIA2_TOKEN）：
          body.params = [f"token:{token}", *params]
    提交下载前检查 numActive / numWaiting（§4.4 步骤 6 前忙闲检查）；
    add_uri 的 comment 传 "lumencloud:<media_id>:<episode>" 标记 GID 来源。
    2026-09 修订：实测 aria2 1.36.0 静默丢弃 comment option（getOption/tellStatus
    均读不到），故 GID 来源校验（§12.2 防 n8n 误启动双转存）已改为 **DB gid
    白名单**（transfer._admit_batch 段 2：aria2 活动/等待任务 gid 必须在
    download_queue 已签发集合内）；comment 仅作未来 aria2 版本兼容的冗余标记。
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(30.0)

# tellStatus 关注的字段（状态轮询 / GID 来源校验 / 实时进度都需要）
_DEFAULT_KEYS = [
    "gid",
    "status",
    "errorCode",
    "errorMessage",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "followedBy",
    "comment",
]


class Aria2Unavailable(Exception):
    """aria2 服务不可用：配置缺失 / 网络故障 / RPC 错误。"""


class Aria2Client:
    """aria2 客户端（下沉为类，便于注入 mock 与单测）。"""

    def __init__(self) -> None:
        # Phase 8 配置入库：RPC 端点/token 改为属性每次访问经 config_store 读取
        # （DB 优先、env fallback），PATCH 保存后无需重启即生效。
        logger.debug("Aria2Client 初始化（RPC 端点%s）", self._rpc_url or "未配置")

    @property
    def _rpc_url(self) -> Optional[str]:
        return (config_store.get("aria2_rpc_url", settings.ARIA2_RPC_URL) or "").strip() or None

    @property
    def _token(self) -> Optional[str]:
        return (config_store.get("aria2_token", settings.ARIA2_TOKEN) or "").strip() or None

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        """JSON-RPC 2.0 调用：返回响应的 result（结构随方法而变）。

        异常 → Aria2Unavailable：配置缺失 / HTTPError / 非 2xx / JSON 异常 /
        JSON-RPC 业务错误（响应含 "error" 字段，如 1=已存在同 URI 任务）。
        """
        if not self._rpc_url:
            raise Aria2Unavailable("ARIA2_RPC_URL 未配置，aria2 RPC 不可用")

        # token 前缀归一化（2026-09 修复）：配置值可能已带 `token:` 前缀
        # （如 .env 的 ARIA2_TOKEN=token:xxx），无脑再拼一次会变 `token:token:xxx`
        # → aria2 拒绝 → HTTP 400 → 下载队列永远 pending。已带前缀则原样透传，
        # 否则补前缀；空值保持传 ""（与修复前行为一致）。
        raw_token = (self._token or "").strip()
        if raw_token:
            secret = raw_token if raw_token.startswith("token:") else f"token:{raw_token}"
        else:
            secret = ""

        body = {
            "jsonrpc": "2.0",
            "id": "lumencloud",
            "method": method,
            "params": [secret, *params],
        }
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(self._rpc_url, json=body)
            except httpx.HTTPError as exc:
                logger.warning("aria2 RPC %s 请求失败: %s", method, exc)
                raise Aria2Unavailable(f"aria2 RPC 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            logger.warning("aria2 RPC %s 非 2xx: %s", method, resp.status_code)
            raise Aria2Unavailable(f"aria2 RPC 返回 HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise Aria2Unavailable("aria2 RPC 响应不是合法 JSON") from exc

        if "error" in payload:
            err = payload.get("error") or {}
            code = err.get("code")
            message = err.get("message") or err
            logger.warning("aria2 RPC %s 业务错误: code=%s %s", method, code, message)
            raise Aria2Unavailable(f"aria2 RPC 错误: {message}")

        return payload.get("result")

    async def add_uri(
        self,
        uri: str,
        *,
        download_dir: Optional[str] = None,
        out: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> str:
        """aria2.addUri：返回 GID，供 download_task.aria2_gid 跟踪。

        参数:
            uri:          直链（alist.get_link 产出）
            download_dir: 下载目录（settings / 任务级覆盖）
            out:          落盘文件名
            comment:      来源标记，本系统传 "lumencloud:<media_id>:<episode>"
                          （2026-09 修订：aria2 1.36.0 会静默丢弃该 option，
                          GID 来源校验已改用 DB gid 白名单，此参数仅作未来
                          aria2 版本兼容的冗余标记）
        """
        options: dict[str, str] = {}
        if download_dir is not None:
            options["dir"] = download_dir
        if out is not None:
            options["out"] = out
        if comment is not None:
            options["comment"] = comment

        params: list[Any] = [[uri]]
        if options:
            params.append(options)

        result = await self._rpc("aria2.addUri", params)
        return str(result)

    async def tell_status(self, gid: str) -> dict[str, Any]:
        """aria2.tellStatus：轮询下载状态（active/waiting/paused/error/complete/removed）。

        参数必须平铺 [gid, keys]（曾误写 [[gid], keys] 把 gid 包成数组 → aria2 拒绝
        → HTTP 400 → 下载完成状态永远轮询不到，任务卡在 downloading 直至超时回滚）。
        """
        result = await self._rpc("aria2.tellStatus", [gid, list(_DEFAULT_KEYS)])
        return result or {}

    async def get_global_stat(self) -> dict[str, Any]:
        """aria2.getGlobalStat：numActive/numWaiting/numStopped/downloadSpeed/uploadSpeed。"""
        result = await self._rpc("aria2.getGlobalStat", [])
        return result or {}

    async def tell_active(self) -> list[dict[str, Any]]:
        """aria2.tellActive：活动任务列表（转存前 GID 来源校验用，见 §12.2）。

        每项含 {gid, status, comment, totalLength, completedLength}；comment 字段在
        aria2 1.36.0 下恒为空（addUri 丢弃该 option）。校验逻辑（transfer.
        _admit_batch 段 2）：aria2 活动/等待任务 gid 必须在本系统 download_queue
        已签发 gid 集合内才继续转存，发现陌生 gid 本轮跳过并告警。
        """
        result = await self._rpc(
            "aria2.tellActive",
            [["gid", "status", "comment", "totalLength", "completedLength"]],
        )
        return result or []

    async def tell_waiting(self, offset: int = 0, num: int = 100) -> list[dict[str, Any]]:
        """aria2.tellWaiting：等待队列任务列表（转存前 GID 来源校验用，§12.2）。

        P2-6（council）：waiting 队列中的陌生任务同样代表 n8n 误启动（排队中的
        双转存），仅校验 tell_active 会漏检。返回字段同 tell_active
        （gid/status/comment/totalLength/completedLength），transfer 层将
        active + waiting 合并后做统一来源校验（gid 白名单口径，见 tell_active）。

        参数:
            offset: 起始位置偏移（默认 0）
            num:    返回条数上限（默认 100）
        """
        result = await self._rpc(
            "aria2.tellWaiting",
            [
                offset,
                num,
                ["gid", "status", "comment", "totalLength", "completedLength"],
            ],
        )
        return result or []

    async def remove(self, gid: str) -> dict[str, Any]:
        """aria2.remove：移除下载任务（recovery 超时回退时清理残留任务）。

        返回移除的 gid 对象；任务不存在/已移除时 aria2 返回 error（由 _rpc 归一为
        Aria2Unavailable，调用方 try/except 兜底不阻断回退流程）。
        """
        result = await self._rpc("aria2.remove", [gid])
        return result or {}


# 模块级单例（与 cloudsaver 的模块级 token 缓存同风格）
client = Aria2Client()