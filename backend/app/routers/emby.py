"""Emby 影视库 API（des-3 Emby 展示页），登录用户可调。

- GET /api/emby/library?item_type=movie|series&status=continuing|ended&anime=true
  → Emby 库条目列表（item_type 类型筛选 / status 在更完结 / anime 动漫库）
  200 → {"items": [...], "total": n, "item_type": "movie"|"series"|null}
- GET /api/emby/libraries → Emby 媒体库列表（/Library/VirtualFolders，动漫识别用）
  200 → {"libraries": [...], "total": n}
- Emby 服务不可用 → 503 {"detail": {"msg": "...", "code": "..."}}
  code 取值与前端 stores/emby.ts parseEmbyErrorCode 对齐：
    emby_not_configured（配置缺失 → 前端「未配置空态」）
    emby_unreachable（网络故障/非 2xx → 前端「不可达错误态」）
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import User
from app.routers.deps import get_current_user
from app.services.emby import EmbyUnavailable, list_libraries, list_library

router = APIRouter(prefix="/emby", tags=["emby"])


def _to_http_exc(exc: EmbyUnavailable) -> HTTPException:
    """EmbyUnavailable → 503，code 与前端 parseEmbyErrorCode 对齐。"""
    msg = str(exc)
    # 配置缺失（EMBY_BASE_URL / EMBY_API_KEY 未配置）→ 前端「未配置空态」
    if "未配置" in msg:
        return HTTPException(status_code=503, detail={"msg": msg, "code": "emby_not_configured"})
    # 网络故障 / 非 2xx / JSON 异常 → 前端「不可达错误态」
    return HTTPException(status_code=503, detail={"msg": msg, "code": "emby_unreachable"})


@router.get("/library")
async def library(
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    anime: bool = False,
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 影视库展示：item_type=movie/series；status=continuing 在更 / ended 完结
    （仅对剧集生效，后端保证 IncludeItemTypes 含 Series）；anime=true 限定动漫库
    （按 Name 关键词匹配，忽略 item_type 过滤）。"""
    try:
        items = await list_library(item_type, status, anime)
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "items": items,
        "total": len(items),
        "item_type": item_type,
    }


@router.get("/libraries")
async def libraries(
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 媒体库列表（/Library/VirtualFolders）：供前端动漫 Tab 初始化/展示可选
    （当前前端用固定 Tab，后端 Name 匹配动漫库，本端点留作扩展）。"""
    try:
        libs = await list_libraries()
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "libraries": libs,
        "total": len(libs),
    }
