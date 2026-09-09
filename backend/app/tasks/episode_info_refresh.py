"""每日刷新 TMDB 集信息缓存（episode-status-cache）。

遍历全部 tv media（tmdb_id 非空）调用 services.tmdb.refresh_episode_info，
单影视失败跳过并 log warning，不中断全量刷新。间隔由 system_config
episode_info_refresh_interval_hours（默认 24h）控制，job 开关沿用 scheduler 双层开关。
"""
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import Media
from app.services.tmdb import refresh_episode_info

logger = logging.getLogger(__name__)


async def episode_info_refresh() -> int:
    """全量刷新集信息缓存；返回成功刷新的影视数。"""
    async with async_session() as s:
        rows = (
            (await s.execute(
                select(Media.id, Media.tmdb_id).where(
                    Media.media_type == "tv",
                    Media.tmdb_id.is_not(None),
                )
            ))
            .all()
        )
    ok = 0
    for _mid, tmdb_id in rows:
        try:
            n = await refresh_episode_info(tmdb_id)
            if n > 0:
                ok += 1
        except Exception as exc:  # noqa: BLE001  单影视失败跳过，不影响其余
            logger.warning("[episode_info_refresh] media tmdb=%s 刷新失败跳过: %s", tmdb_id, exc)
    return ok


async def episode_info_refresh_job() -> None:
    """APScheduler job 包装（每日一次）：异常不外泄，不影响调度器其余 job。"""
    try:
        await episode_info_refresh()
    except Exception:  # noqa: BLE001
        logger.exception("[episode_info_refresh] 定时任务异常")
