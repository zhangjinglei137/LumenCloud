"""业务 API 聚合路由。

聚合阶段 3 全部子路由并导出 api_router，由 app.main 引入注册：
    from app.routers.api import api_router
    app.include_router(api_router)
"""
from fastapi import APIRouter

from app.routers import (
    admin,
    approvals,
    auth,
    capacity,
    emby,
    logs,
    media,
    notifications,
    queue,
    settings,
    tmdb,
)

api_router = APIRouter(prefix="/api")

api_router.include_router(auth.router)
api_router.include_router(media.router)
api_router.include_router(queue.router)
api_router.include_router(capacity.router)
api_router.include_router(tmdb.router)
api_router.include_router(emby.router)
api_router.include_router(approvals.router)
api_router.include_router(settings.router)
api_router.include_router(logs.router)
api_router.include_router(admin.router)
api_router.include_router(notifications.router)

__all__ = ["api_router"]