"""Emby 影视库 API（des-3 Emby 展示页），登录用户可调。

- GET /api/emby/library?item_type=movie|series → Emby 库条目列表
  200 → {"items": [...], "total": n, "item_type": "movie"|"series"|null}
- Emby 服务不可用 → 503 {"detail": {"msg": "...", "code": "..."}}
  code 取值与前端 stores/emby.ts parseEmbyErrorCode 对齐：
    emby_not_configured（配置缺失 → 前端「未配置空态」）
    emby_unreachable（网络故障/非 2xx → 前端「不可达错误态」）
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import User
from app.routers.deps import get_current_user
from app.services.emby import EmbyUnavailable, list_library

router = APIRouter(prefix="/emby", tags=["emby"])


@router.get("/library")
async def library(
    item_type: Literal["movie", "series"] | None = Query(default=None),
    user: User = Depends(get_current_user),  # 登录用户可调（非 admin 限定）
) -> dict:
    """Emby 影视库展示：item_type=movie/series，缺省为全部（Movie,Series）。"""
    try:
        items = await list_library(item_type)
    except EmbyUnavailable as exc:
        msg = str(exc)
        # 配置缺失（EMBY_BASE_URL / EMBY_API_KEY 未配置）→ 前端「未配置空态」
        if "未配置" in msg:
            raise HTTPException(status_code=503, detail={"msg": msg, "code": "emby_not_configured"}) from exc
        # 网络故障 / 非 2xx / JSON 异常 → 前端「不可达错误态」
        raise HTTPException(status_code=503, detail={"msg": msg, "code": "emby_unreachable"}) from exc
    return {
        "items": items,
        "total": len(items),
        "item_type": item_type,
    }
