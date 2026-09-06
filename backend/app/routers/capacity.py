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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QuarkCapacityLog, User
from app.routers.deps import get_current_user, get_session
from app.routers.queue import _load_transfer_queue_model

logger = logging.getLogger(__name__)

router = APIRouter()

_GB = 1024**3

# 阶段 4 生产化 / E：最近快照窗口（交付 3）——最近 24h 内、上限 48 条，供前端趋势图
_RECENT_SNAPSHOT_HOURS = 24
_RECENT_SNAPSHOT_LIMIT = 48


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
    - usage_rate：used_gb/total_gb 使用率（0~1；不可用/无 quota 时为 None）。
    - recent_snapshots：最近 24h 容量快照（上限 48 条，时间升序；Q6 容量长期趋势）。
    """
    usage = await _get_usage()

    source = usage.get("source") or "unavailable"
    total_gb = usage.get("total_gb")
    used_gb = usage.get("used_gb")
    checked_at = usage.get("checked_at")
    error = usage.get("error")

    # 阶段 4 生产化 / E：使用率（total_gb 为 0/空 → None，不除零）
    usage_rate = None
    if used_gb is not None and total_gb:
        usage_rate = round(used_gb / total_gb, 4)

    return {
        "total_gb": total_gb,
        "used_gb": used_gb,
        "source": source,
        "checked_at": checked_at,
        "error": error,
        "pending_estimate": await _pending_estimate_gb(session),
        "usage_rate": usage_rate,
        "recent_snapshots": await _recent_snapshots(session),
    }


# 阶段 4 生产化 / E：最近快照查询（交付 3，Q6 容量长期趋势数据）
async def _recent_snapshots(session: AsyncSession) -> list[dict]:
    """最近 24h 容量快照（上限 48 条，时间升序；供前端趋势图直接按序绘制）。

    数据源 quark_capacity_log（60s 节流 + 每小时巡检兜底落库）；查询失败不抛
    500（趋势数据为增强字段，失败回退空列表并告警日志）。
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        hours=_RECENT_SNAPSHOT_HOURS
    )
    try:
        rows = (
            (
                await session.execute(
                    select(QuarkCapacityLog)
                    .where(
                        QuarkCapacityLog.checked_at.isnot(None),
                        QuarkCapacityLog.checked_at >= since,
                    )
                    .order_by(
                        QuarkCapacityLog.checked_at.desc(),
                        QuarkCapacityLog.id.desc(),
                    )
                    .limit(_RECENT_SNAPSHOT_LIMIT)
                )
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001  趋势数据为增强字段，失败不 500
        logger.warning("/api/capacity 读取最近快照失败（返回空列表）: %s", exc)
        return []
    snapshots = [
        {"checked_at": row.checked_at, "used_gb": row.used_gb, "total_gb": row.total_gb}
        for row in rows
    ]
    snapshots.reverse()  # 升序（旧→新），前端趋势图直接按序绘制
    return snapshots


async def _get_usage() -> dict:
    """调用 CapacityProvider.get_usage；容量不可用 → fail-closed 归一化响应（不 500）。

    复用模块级单例 provider（C-3）：进程内 30s 用量缓存（USAGE_CACHE_TTL_SECONDS）
    与 60s 快照节流（SNAPSHOT_THROTTLE_SECONDS）均为实例级状态，只有复用单例才
    生效；每请求新建 Provider 会绕过缓存，前端高频轮询时每次重复全量递归
    alist /quark 目录树。
    """
    try:
        # C-3：复用模块级单例（而非每请求实例化 CapacityProvider）——吃到
        # USAGE_CACHE_TTL_SECONDS 用量缓存与 SNAPSHOT_THROTTLE_SECONDS 快照节流，
        # 避免前端高频轮询导致每次全量递归 alist /quark 目录树。
        from app.services.capacity import CapacityInfo, provider
    except ImportError:
        return {
            "source": "unavailable",
            "error": "容量提供者未就绪",
            "total_gb": None,  # Q3：数值字段不可用时为 null（前端 JSON 契约稳定）
            "used_gb": None,
        }
    try:
        usage = await provider.get_usage()
    except Exception as exc:  # CapacityUnavailable 及其它 → fail-closed 归一化
        logger.warning("容量读取失败，/api/capacity 返回 unavailable: %s", exc)
        return {
            "source": "unavailable",
            "error": "容量数据不可用",
            "total_gb": None,  # Q3：数值字段不可用时为 null（前端 JSON 契约稳定）
            "used_gb": None,
        }
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