"""Emby 影视库 API（des-3 Emby 展示页），登录用户可调。

- GET /api/emby/library?library_id=<MediaFolders Id>&item_type=movie|series&status=continuing|ended
  → Emby 库条目列表（library_id 必选：目标媒体库；item_type 类型筛选 / status 在更完结）
  200 → {"items": [...], "total": n, "item_type": "movie"|"series"|null}
- GET /api/emby/libraries → Emby 媒体库列表（/Library/MediaFolders，含
  CollectionType/is_anime，供分类 Tab 与设置页多选）
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
from app.services.emby import EmbyUnavailable, list_all_library, list_library, list_library_folders

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
    library_id: str = Query(...),
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 影视库展示：library_id 必选（目标媒体库）；item_type=movie/series；
    status=continuing 在更 / ended 完结（仅对剧集生效）。"""
    try:
        items = await list_library(library_id, item_type, status)
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "items": items,
        "total": len(items),
        "item_type": item_type,
    }


@router.get("/library/all")
async def library_all(
    item_type: Literal["movie", "series"] | None = Query(default=None),
    status: Literal["continuing", "ended"] | None = Query(default=None),
    user: User = Depends(get_current_user),  # 登录用户可调
) -> dict:
    """全部影视类库聚合查询：一次请求遍历全部影视类库，按 emby_id 去重返回。

    参数/响应/错误契约与 /library 一致（item_type/status 可选；item_type 回显）。
    """
    try:
        items = await list_all_library(item_type, status)
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
    """Emby 媒体库列表（/Library/MediaFolders，含 CollectionType/is_anime）：
    供前端影视库分类 Tab 生成与设置页「剧集页可见媒体库」多选。"""
    try:
        libs = await list_library_folders()
    except EmbyUnavailable as exc:
        raise _to_http_exc(exc) from exc
    return {
        "libraries": libs,
        "total": len(libs),
    }
