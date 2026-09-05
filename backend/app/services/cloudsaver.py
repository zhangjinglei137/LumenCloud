"""
cloudSaver 网盘搜源 / 分享信息 / 转存服务。

API 契约（由主控探明 + n8n 旧流程，docs/新系统设计.md §10）：
    POST {base}/api/user/login      {username, password} → {success, data:{token}}，token 有效期 6h
    GET  {base}/api/search?keyword=...                     → 搜源
    GET  {base}/api/quark/share-info?shareCode=...&receiveCode=<提取码>
                                                           → 分享信息（含 stoken/fids/fid_tokens/folder_id）
    POST {base}/api/quark/share-list                       → 生产版扩展端点（n8n 在用，递归列目录）
    POST {base}/api/quark/save  {fids, fidTokens, folderId, shareCode, receiveCode}
                                                           → 转存，receiveCode 语义 = stoken
鉴权：Authorization: Bearer <token>
Token 管理：
    - **只进内存**（模块级缓存，不落库、不落 Redis）
    - asyncio.Lock 串行化 login，防并发重复登录
    - 请求遇 401/403（token 失效）→ 清缓存 → 自动重新 login 重试一次
调用方约定：
    - share-info 逐分享码串行调用、500ms 间隔由调用方控制（本层提供独立方法）
"""
import asyncio
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(10.0)
SEARCH_TIMEOUT = httpx.Timeout(90.0)      # 搜索聚合多频道，实测固定 ~40s，超时须充足
SHARE_INFO_TIMEOUT = httpx.Timeout(5.0)   # 逐码串行，单次超时不宜过长
SAVE_TIMEOUT = httpx.Timeout(30.0)        # 转存可能较慢（n8n 契约 30s）

AUTH_EXPIRED_STATUS = {401, 403}          # token 失效判定


class CloudSaverUnavailable(Exception):
    """cloudSaver 服务不可用：配置缺失 / 网络故障 / 非预期响应。"""


class CloudSaverAuthError(Exception):
    """登录失败 / 凭据错误（区别于服务不可用，供上层告警 N2）。"""


# ---- token 内存缓存 ----
_token: Optional[str] = None
_token_lock: asyncio.Lock = asyncio.Lock()


def _base_url() -> str:
    base = (settings.CLOUDSAVER_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise CloudSaverUnavailable("CLOUDSAVER_BASE_URL 未配置")
    return base


def _check_login_config() -> None:
    if not settings.CLOUDSAVER_USERNAME or not settings.CLOUDSAVER_PASSWORD:
        raise CloudSaverUnavailable("CLOUDSAVER_USERNAME/PASSWORD 未配置")


def _invalidate_token() -> None:
    global _token
    if _token:
        logger.info("cloudSaver token 失效，已清除内存缓存")
    _token = None


async def _login() -> str:
    """调用 /api/user/login 换取 token（成功与否都保证锁内只执行一次）。"""
    _check_login_config()
    url = f"{_base_url()}/api/user/login"
    body = {
        "username": settings.CLOUDSAVER_USERNAME,
        "password": settings.CLOUDSAVER_PASSWORD,
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            logger.warning("cloudSaver login 请求失败: %s", exc)
            raise CloudSaverUnavailable(f"cloudSaver login 请求失败: {exc}") from exc

    if resp.status_code >= 400:
        logger.warning("cloudSaver login 非 2xx: %s", resp.status_code)
        raise CloudSaverUnavailable(f"cloudSaver login 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudSaverUnavailable("cloudSaver login 响应不是合法 JSON") from exc

    if not payload.get("success"):
        logger.error("cloudSaver login 失败: %s", payload.get("message"))
        raise CloudSaverAuthError(payload.get("message") or "cloudSaver login 失败")

    token = (payload.get("data") or {}).get("token")
    if not token:
        raise CloudSaverAuthError("cloudSaver login 响应缺少 token")
    logger.info("cloudSaver token 获取成功（6h 有效期，仅驻留内存）")
    return token


async def _get_token() -> str:
    """取 token；缓存为空则加锁 login（double-check 防并发重复登录）。"""
    global _token
    if _token:
        return _token
    async with _token_lock:
        if _token:
            return _token
        _token = await _login()
        return _token


async def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json: Optional[dict[str, Any]] = None,
    timeout: httpx.Timeout = REQUEST_TIMEOUT,
) -> httpx.Response:
    """带鉴权的请求封装：token 失效（401/403）时自动重新 login 并重试一次。"""
    token = await _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{_base_url()}{path}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.request(method, url, params=params, json=json, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("cloudSaver 请求失败 %s %s: %s", method, path, exc)
            raise CloudSaverUnavailable(f"cloudSaver 请求失败: {exc}") from exc

    if resp.status_code in AUTH_EXPIRED_STATUS:
        logger.warning("cloudSaver token 失效（HTTP %s），重新 login 重试一次", resp.status_code)
        _invalidate_token()
        token = await _get_token()
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.request(method, url, params=params, json=json, headers=headers)
            except httpx.HTTPError as exc:
                raise CloudSaverUnavailable(f"cloudSaver 重试请求失败: {exc}") from exc
    return resp


async def _request_success(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json: Optional[dict[str, Any]] = None,
    timeout: httpx.Timeout = REQUEST_TIMEOUT,
) -> Any:
    """_request + 响应归一：非 2xx / success=false / JSON 异常统一抛 CloudSaverUnavailable。"""
    resp = await _request(method, path, params=params, json=json, timeout=timeout)
    if resp.status_code >= 400:
        logger.warning("cloudSaver 非 2xx %s: %s", path, resp.status_code)
        raise CloudSaverUnavailable(f"cloudSaver 返回 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise CloudSaverUnavailable("cloudSaver 响应不是合法 JSON") from exc
    if not payload.get("success"):
        msg = payload.get("message") or "cloudSaver 响应 success=false"
        logger.warning("cloudSaver 业务失败 %s: %s", path, msg)
        raise CloudSaverUnavailable(f"cloudSaver 业务失败: {msg}")
    return payload.get("data") or {}


async def search(keyword: str) -> list[dict[str, Any]]:
    """网盘搜源（GET /api/search）。

    参数:
        keyword: 搜索关键词（影视名称）
    返回:
        归一化结果列表，每项含:
            title:       资源标题（小写去空格，供加分匹配）
            cloud_links: 分享链接列表 [{link, cloud_type}]（cloud_type=quark 时提取分享码）
    异常:
        CloudSaverUnavailable / CloudSaverAuthError

    响应结构（生产版实测）：
        data: [ { list: [ { title, cloudLinks: [{link, cloudType}], ... } ], channelInfo, ... } ]
        分享链接在 channel 内 list 项的 cloudLinks，不在顶层。
    """
    data = await _request_success(
        "GET", "/api/search", params={"keyword": keyword}, timeout=SEARCH_TIMEOUT
    )
    # data: [{list: [{title, cloudLinks: [{link, cloudType}]}]}]
    data_list = data if isinstance(data, list) else []
    results: list[dict[str, Any]] = []
    for channel in data_list:
        if not isinstance(channel, dict):
            continue
        for item in channel.get("list") or []:
            if not isinstance(item, dict):
                continue
            links = []
            for cloud_link in (item.get("cloudLinks") or []):
                if not isinstance(cloud_link, dict):
                    continue
                links.append({
                    "link": cloud_link.get("link"),
                    "cloud_type": cloud_link.get("cloudType"),
                })
            results.append({"title": (item.get("title") or "").lower().strip(), "cloud_links": links})
    logger.info("cloudSaver 搜索「%s」命中 %d 条", keyword, len(results))
    return results


async def share_info(share_code: str, receive_code: Optional[str] = None) -> dict[str, Any]:
    """获取分享信息（GET /api/quark/share-info）。

    参数:
        share_code:  夸克分享码（pan.quark.cn/s/<share_code>）
        receive_code: 分享提取码（passcode，可选；无提取码的分享可省略）
    返回:
        data 字典（含 stoken / fids / fid_tokens / folder_id 等转存凭据）
    异常:
        CloudSaverUnavailable / CloudSaverAuthError
    """
    params: dict[str, Any] = {"shareCode": share_code}
    if receive_code:
        params["receiveCode"] = receive_code
    data = await _request_success("GET", "/api/quark/share-info", params=params, timeout=SHARE_INFO_TIMEOUT)
    return data


async def share_list(
    share_code: str,
    *,
    pdir_fid: str = "",
    pwd_id: str = "",
    stoken: str = "",
    receive_code: str = "",
) -> dict[str, Any]:
    """列分享目录文件（生产版扩展端点 POST /api/quark/share-list，n8n 在用）。

    用于分享内文件夹递归遍历（如季目录），契约与 n8n「提取缺失集」节点一致。

    参数:
        share_code:   分享码
        pdir_fid:     父目录 fid（递归时传文件 id；根目录传空串）
        pwd_id / stoken: 来自 share_info 响应的凭据
        receive_code: 提取码（此处透传，语义由端点约定）
    返回:
        data 字典（含 list: [{fileName, fileId, fileIdToken, isFolder, size}]）
    异常:
        CloudSaverUnavailable / CloudSaverAuthError
    """
    payload = {
        "pwdId": pwd_id,
        "stoken": stoken,
        "pdirFid": pdir_fid,
        "shareCode": share_code,
        "receiveCode": receive_code,
        "shareInfo": {
            "list": [],
            "pwdId": pwd_id,
            "stoken": stoken,
            "fileSize": 0,
            "shareCode": share_code,
            "type": "quark",
        },
    }
    return await _request_success("POST", "/api/quark/share-list", json=payload)


async def save(params: dict[str, Any]) -> dict[str, Any]:
    """转存到夸克中转空间（POST /api/quark/save）。

    参数:
        params: 转存参数，键含:
            fids:        待转存文件 id 列表
            fidTokens:   与 fids 等长的 fid token 列表（可空串补位）
            folderId:    目标文件夹 id（夸克中转目录）
            shareCode:   来源分享码
            receiveCode: **语义 = stoken**（来自 share_info 响应，勿传提取码）
    返回:
        data 字典（如 {task_id}）
    异常:
        CloudSaverUnavailable / CloudSaverAuthError
    """
    if not isinstance(params, dict) or not params.get("fids"):
        raise ValueError("save 参数缺少 fids")
    return await _request_success("POST", "/api/quark/save", json=params, timeout=SAVE_TIMEOUT)
