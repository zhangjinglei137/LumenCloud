"""影视海报代理 API。

- GET /api/poster?p=<相对路径>：登录用户可调（cookie 兜底，<img> 同源可用）
- 校验非法 → 400；配置误填 → 503；回源失败 → 502；未登录 → 401
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response as FastAPIResponse

from app.models import User
from app.routers.deps import get_current_user
from app.services.poster import PosterUnavailable, _validate_poster_path, fetch_poster

router = APIRouter(prefix="/poster", tags=["poster"])


@router.get("")
async def get_poster(
    p: str = Query(min_length=1, max_length=512),
    user: User = Depends(get_current_user),
):
    """代理拉取 TMDB 图床海报图片。"""
    if not _validate_poster_path(p):
        # 返回 400 Response 而非 raise HTTPException：路由函数直调测试
        # 断言 resp.status_code==400（brief 验收形式）；ASGI 层语义与
        # raise HTTPException 一致（400 + {"detail": ...}）。
        return JSONResponse(status_code=400, content={"detail": "非法海报路径"})
    try:
        content, content_type = await fetch_poster(p)
    except PosterUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  回源失败统一 502
        raise HTTPException(status_code=502, detail=f"海报代理拉取失败: {exc}") from exc
    return FastAPIResponse(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
