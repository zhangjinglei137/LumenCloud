"""TMDB 搜索 API（docs/新系统设计.md §9.2），登录用户可调。

- GET /api/tmdb/search?q= → tmdb.search_multi 归一化结果（title/tmdb_id/media_type/poster_path）
- TMDB 服务不可用（TMDBUnavailable）→ 503 {"detail": msg}
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import User
from app.routers.deps import get_current_user
from app.services.tmdb import TMDBUnavailable, search_multi

router = APIRouter(prefix="/tmdb", tags=["tmdb"])


@router.get("/search")
async def search(
    q: str = Query(min_length=1, max_length=200),
    user: User = Depends(get_current_user),  # 登录用户可调
) -> list[dict]:
    """多类型影视搜索（movie/tv/person）。"""
    try:
        results = await search_multi(q)
    except TMDBUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return results