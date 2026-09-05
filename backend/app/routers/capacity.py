"""容量状态 API。

GET /api/capacity —— 夸克容量（used/total/source/checked_at）+
pending 队列预估占用（pending = 未消费积压，不代表真实占用）。

容量语义（阶段 3）：
- source=alist   → 实时 /quark 目录统计（真实占用）
- source=unavailable → 实时容量读取失败（fail-closed），前端展示为「容量不可用」，
  转存决策由 transfer 的 CapacityProvider.check fail-closed 决定（绝不在此放行）
- 历史 estimated 来源已由 provider 真实化取代（Q1 实证：容量数据源 = alist）
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.routers.deps import get_current_user, get_session
from app.routers.queue import _load_transfer_queue_model

logger = logging.getLogger(__name__)

router = APIRouter()

_GB = 1024**3


@router.get("/capacity")
async def capacity(
    user: User = Depends(get_current_user),  # §9.1 登录即可读容量；匿名 → 401
    session: AsyncSession = Depends(get_session),
) -> dict:
    """容量状态。

    - pending_estimate：transfer_queue 中 status=pending 的 file_size 之和（折 GB）；
      **pending = 未消费积压，不代表真实占用**（仅搜索入队，尚未转存/下载）。
    - 实时容量不可用（alist 故障/未配置）→ source=unavailable、used_gb=None
      （fail-closed 展示，不误导；不抛 500）。
    """
    usage = await _get_usage()

    source = usage.get("source") or "unavailable"
    total_gb = usage.get("total_gb")
    used_gb = usage.get("used_gb")
    checked_at = usage.get("checked_at")
    error = usage.get("error")

    return {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "source": source,
        "checked_at": checked_at,
        "error": error,
        "pending_estimate": await _pending_estimate_gb(session),
    }


async def _get_usage() -> dict:
    """调用 CapacityProvider.get_usage；容量不可用 → fail-closed 归一化响应（不 500）。"""
    try:
        from app.services.capacity import CapacityInfo, CapacityProvider
    except ImportError:
        return {"source": "unavailable", "error": "容量提供者未就绪"}
    try:
        usage = await CapacityProvider().get_usage()
    except Exception as exc:  # CapacityUnavailable 及其它 → fail-closed 归一化
        logger.warning("容量读取失败，/api/capacity 返回 unavailable: %s", exc)
        return {"source": "unavailable", "error": "容量数据不可用"}
    if isinstance(usage, CapacityInfo):
        return {
            "source": usage.source,
            "total_gb": usage.total_gb,
            "used_gb": usage.used_gb,
            "checked_at": usage.checked_at,
        }
    return dict(usage)


async def _pending_estimate_gb(session: AsyncSession) -> float | None:
    """pending 队列 file_size（字节）之和 → GB。

    pending = 未消费积压，不代表真实占用，仅作参考预估。
    """
    TransferQueue = _load_transfer_queue_model()
    if TransferQueue is None:
        # 骨架期模型未建
        return None
    total_bytes = (
        await session.execute(
            select(func.coalesce(func.sum(TransferQueue.file_size), 0)).where(
                TransferQueue.status == "pending"
            )
        )
    ).scalar_one()
    return round(total_bytes / _GB, 2)