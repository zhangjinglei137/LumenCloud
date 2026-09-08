"""业务 API 聚合路由。

聚合阶段 3 全部子路由并导出 api_router，由 app.main 引入注册：
    from app.routers.api import api_router
    app.include_router(api_router)

顶层 router 无前缀：业务路由统一挂 /api 前缀子 router（对外契约不变）；P6 aria2
hook 回调（POST /internal/aria2/notify）作为「无 /api 前缀」的内部端点挂入同一顶层
router——设计文档 §6.2 明确回调地址为 http://<backend>/internal/aria2/notify，
aria2 主机 notify.sh 直连不走 /api（否则会因 api_router 的 /api 前缀偏移成
/api/internal/... 而 404）。
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
    nastools_notify,
    notifications,
    notify,
    queue,
    settings,
    tmdb,
)

# 业务 API：统一 /api 前缀（保持对外契约不变）
_business_router = APIRouter(prefix="/api")

_business_router.include_router(auth.router)
_business_router.include_router(media.router)
_business_router.include_router(queue.router)
_business_router.include_router(capacity.router)
_business_router.include_router(tmdb.router)
_business_router.include_router(emby.router)
_business_router.include_router(approvals.router)
_business_router.include_router(settings.router)
_business_router.include_router(logs.router)
_business_router.include_router(admin.router)
_business_router.include_router(notifications.router)

# 顶层聚合 router（main.py 引入注册的对象）：业务 /api + 内部 /internal 回调
api_router = APIRouter()
api_router.include_router(_business_router)
api_router.include_router(notify.router)
api_router.include_router(nastools_notify.router)

__all__ = ["api_router"]
