"""
AList 挂载 / 直链 / 释放服务。

契约（docs/新系统设计.md §10，n8n 旧流程）：
    POST {base}/api/fs/list   {path, password:"", page, per_page}  → 列目录
    POST {base}/api/fs/remove {dir, names}                         → 释放文件
    POST {base}/api/fs/get    {path}                               → 取直链（data.url）
鉴权：Authorization: settings.ALIST_TOKEN（alist 原始 token，无需 Bearer 前缀，与 n8n 一致）
响应约定：alist 统一返回 {code:200, message, data}；code 非 200 视为业务失败。
"""
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(10.0)


class AlistUnavailable(Exception):
    """AList 服务不可用：配置缺失 / 网络故障 / 非 2xx / 业务失败（code≠200）。"""


def _base_url() -> str:
    base = (settings.ALIST_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise AlistUnavailable("ALIST_BASE_URL 未配置")
    return base


def _headers() -> dict[str, str]:
    if not settings.ALIST_TOKEN:
        raise AlistUnavailable("ALIST_TOKEN 未配置")
    return {"Authorization": settings.ALIST_TOKEN}


async def _post(path: str, body: dict[str, Any], timeout: httpx.Timeout = REQUEST_TIMEOUT) -> dict[str, Any]:
    """POST 封装：鉴权 + 故障归一（网络异常/非 2xx/JSON 异常/业务失败 → AlistUnavailable）。"""
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=body, headers=_headers())
        except httpx.HTTPError as exc:
            logger.warning("AList 请求失败 %s: %s", path, exc)
            raise AlistUnavailable(f"AList 请求失败: {exc}") from exc

    if resp.status_code >= 400:
        logger.warning("AList 非 2xx %s: %s", path, resp.status_code)
        raise AlistUnavailable(f"AList 返回 HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise AlistUnavailable("AList 响应不是合法 JSON") from exc

    if payload.get("code") != 200:
        logger.warning("AList 业务失败 %s: %s", path, payload.get("message"))
        raise AlistUnavailable(f"AList 业务失败: {payload.get('message')}")

    return payload.get("data") or {}


async def list_dir(path: str) -> list[dict[str, Any]]:
    """列目录（POST /api/fs/list）。

    参数:
        path: alist 挂载路径（如 /quark）
    返回:
        目录项列表，每项含:
            name:   文件名/目录名
            is_dir: 是否目录
            size:   字节数（目录通常为 0）
    异常:
        AlistUnavailable
    """
    data = await _post("/api/fs/list", {
        "path": path,
        "password": "",
        "refresh": True,
        "page": 1,
        "per_page": 1000,
    })
    entries: list[dict[str, Any]] = []
    for item in data.get("content") or []:
        entries.append({
            "name": item.get("name"),
            "is_dir": bool(item.get("is_dir")),
            "size": item.get("size") or 0,
        })
    logger.info("AList 列目录 %s: %d 项", path, len(entries))
    return entries


async def remove(names: list[str], dir: str) -> dict[str, Any]:
    """释放文件（POST /api/fs/remove，下载完成后清理夸克残留）。

    参数:
        names: 待删除的文件/目录名列表
        dir:   所在目录（如 /quark/）
    返回:
        data 字典（alist 删除结果）
    异常:
        AlistUnavailable
    """
    if not names:
        return {}
    return await _post("/api/fs/remove", {"dir": dir, "names": list(names)})


async def get_link(path: str) -> str:
    """取直链（POST /api/fs/get，供 aria2 addUri 下载）。

    参数:
        path: 文件在 alist 的完整路径
    返回:
        可直接下载的直链 URL（data.url）
    异常:
        AlistUnavailable
    """
    data = await _post("/api/fs/get", {"path": path})
    link = data.get("url")
    if not link:
        raise AlistUnavailable(f"AList 未返回直链: {path}")
    return str(link)
