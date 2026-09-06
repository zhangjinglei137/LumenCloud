"""
AList 挂载 / 直链 / 释放服务。

契约（docs/新系统设计.md §10，n8n 旧流程）：
    POST {base}/api/fs/list   {path, password:"", page, per_page}  → 列目录
    POST {base}/api/fs/remove {dir, names}                         → 释放文件
    POST {base}/api/fs/get    {path}                               → 取直链（data.url）
鉴权：Authorization: settings.ALIST_TOKEN（alist 原始 token，无需 Bearer 前缀，与 n8n 一致）
响应约定：alist 统一返回 {code:200, message, data}；code 非 200 视为业务失败。
"""
import json
import logging
from typing import Any, Optional

import httpx

from app.config import settings
from app.services import config_store

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = httpx.Timeout(10.0)


class AlistUnavailable(Exception):
    """AList 服务不可用：配置缺失 / 网络故障 / 非 2xx / 业务失败（code≠200）。"""


def _base_url() -> str:
    # Phase 8 配置入库：DB（config_store）优先、env fallback；函数内读取，
    # 每次调用读最新值（PATCH 保存即生效，无需重启）
    base = (config_store.get("alist_base_url", settings.ALIST_BASE_URL) or "").strip().rstrip("/")
    if not base:
        raise AlistUnavailable("ALIST_BASE_URL 未配置")
    return base


def _headers() -> dict[str, str]:
    token = config_store.get("alist_token", settings.ALIST_TOKEN)
    if not token:
        raise AlistUnavailable("ALIST_TOKEN 未配置")
    return {"Authorization": token}


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


async def _get(path: str, timeout: httpx.Timeout = REQUEST_TIMEOUT) -> dict[str, Any]:
    """GET 封装：与 _post 对称（httpx.get + _headers + 错误归一 AlistUnavailable）。"""
    url = f"{_base_url()}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, headers=_headers())
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


async def list_dir(path: str, per_page: int = 1000) -> list[dict[str, Any]]:
    """列目录（POST /api/fs/list，分页循环拉取全部条目）。

    alist 单页最多返回 per_page（默认 1000）条；目录条目超过单页上限时，
    逐页循环拉取，直到某页返回条数 < per_page（含 0 条）即视为最后一页，
    避免目录条目 >1000 时容量统计低估（C-2）。

    参数:
        path: alist 挂载路径（如 /quark）
        per_page: 每页条数（默认 1000，保持既有单页语义）
    返回:
        目录项列表（跨页合并），每项含:
            name:   文件名/目录名
            is_dir: 是否目录
            size:   字节数（目录通常为 0）
    异常:
        AlistUnavailable
    """
    entries: list[dict[str, Any]] = []
    page = 1
    while True:
        # 上限防御：page 超过 2000 直接停止，防止 alist 每页恒满（永远收不到
        # <per_page 的尾页）时 for 循环死循环拉爆。2000 页 × 1000 条 = 200 万
        # 条目，远超单目录实际规模，仅作异常场景兜底。
        if page > 2000:
            logger.warning(
                "AList 列目录 %s: 页数超过 2000 上限，提前停止（已收集 %d 项）",
                path, len(entries),
            )
            break
        data = await _post("/api/fs/list", {
            "path": path,
            "password": "",
            "refresh": True,  # 每页均保持即时刷新语义
            "page": page,
            "per_page": per_page,
        })
        content = data.get("content") or []
        for item in content:
            entries.append({
                "name": item.get("name"),
                "is_dir": bool(item.get("is_dir")),
                "size": item.get("size") or 0,
            })
        # 当前页条数 < per_page → 已到最后一页（含 0 条），停止分页
        if len(content) < per_page:
            break
        page += 1
    logger.info("AList 列目录 %s: %d 项（分页 %d 页）", path, len(entries), page)
    return entries


async def diagnose_quark_mount() -> dict[str, Any]:
    """验证 /quark 挂载与 quark_default_folder 配置一致性（设置页「验证 folderId」）。

    Q1 根因：cloudSaver save 用的 quark_default_folder（folderId）与 alist Quark
    驱动 root_folder_id 不一致 → 文件落盘夸克其他目录 → alist /quark/{file_name}
    永不可见 → 转存链路 _get_link_wait_visible 轮询超时失败。本函数直读 alist
    GET /api/admin/storage/list 的 Quark 驱动 root_folder_id，与 quark_default_folder
    比对返回结构化 match 判定。任何一步失败都捕获为结构化字段，绝不抛异常。
    """
    # 1) 读取配置：DB（config_store）优先、env fallback，与模块内其它函数一致
    base = (config_store.get("alist_base_url", settings.ALIST_BASE_URL) or "").strip().rstrip("/")
    token = (config_store.get("alist_token", settings.ALIST_TOKEN) or "").strip()
    configured = (config_store.get("quark_default_folder", settings.QUARK_DEFAULT_FOLDER)
                  or "").strip() or None

    result: dict[str, Any] = {
        "alist_configured": bool(base and token),
        "quark_mount_found": False,
        "quark_mount_path": None,
        "quark_driver": None,
        "root_folder_id": None,
        "root_folder_status": None,
        "configured_folder_id": configured,
        "match": None,
        "fs_list_ok": False,
        "fs_error": None,
        "quark_files": [],
        "quark_file_count": 0,
        "storage_total": 0,
        "storages": [],
    }
    # 2) alist 未配置：不得发起任何网络请求，直接返回
    if not result["alist_configured"]:
        return result

    # 3) fs/list 探测：/quark 是否挂载可见、可列目录（复用现有 list_dir）
    try:
        entries = await list_dir("/quark")
    except AlistUnavailable as exc:
        result["fs_error"] = str(exc)
    else:
        result["fs_list_ok"] = True
        result["quark_files"] = [e.get("name") for e in entries[:20]]
        result["quark_file_count"] = len(entries)

    # 4) storage 探测：GET /api/admin/storage/list（兼容 alist v3 data.content / v4 data 即列表）
    try:
        data = await _get("/api/admin/storage/list")
    except AlistUnavailable as exc:
        result["quark_mount_found"] = False
        result["fs_error"] = (result["fs_error"] + "; " if result["fs_error"] else "") + \
            f"查询可用挂载失败: {exc}"
    else:
        content = (data.get("content") or []) if isinstance(data, dict) else data
        result["storage_total"] = len(content)
        # 仅返回 mount_path/driver 两字段；addition 含 cookie 等敏感值绝不返回
        result["storages"] = [
            {"mount_path": s.get("mount_path") or None, "driver": s.get("driver") or None}
            for s in content
        ]
        # 匹配 Quark 挂载：优先 mount_path（rstrip("/") 后 == "/quark"），否则按 driver 名
        matched = None
        for s in content:
            if (s.get("mount_path") or "").rstrip("/") == "/quark":
                matched = s
                break
        if matched is None:
            for s in content:
                if (s.get("driver") or "").strip().lower() == "quark":
                    matched = s
                    break
        if matched is not None:
            result["quark_mount_found"] = True
            result["quark_mount_path"] = matched.get("mount_path") or None
            result["quark_driver"] = matched.get("driver") or None
            # 解析 addition（JSON 字符串；解析失败仅两字段 None，不抛）
            raw = matched.get("addition")
            addition: Any = None
            if isinstance(raw, str) and raw:
                try:
                    addition = json.loads(raw)
                except ValueError:
                    addition = None
            elif isinstance(raw, dict):
                addition = raw
            if isinstance(addition, dict):
                result["root_folder_id"] = addition.get("root_folder_id") or None  # 空串也算 None
                status = addition.get("root_folder_status")  # 有键且有值才给，否则 None
                result["root_folder_status"] = (
                    status if isinstance(status, str) and status.strip() else None
                )

    # 5) match 判定：两侧均非空才可比对
    if result["root_folder_id"] is not None and configured is not None:
        result["match"] = bool(result["root_folder_id"] == configured)
    return result


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
