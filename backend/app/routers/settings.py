"""设置 API（admin，docs/新系统设计.md §9.1 配置接口契约）。

- GET  /api/settings  system_config 全量（字符串值）+ services 凭据「是否已配置」布尔
                     —— 绝不回显任何凭据值，只回 {key: bool}
- PATCH /api/settings 白名单键 UPSERT（非敏感键）；含调度相关键时 commit 后
                      事件驱动重新应用 job 开关（M2，Oracle Gate2）
"""
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import SystemConfig, User
from app.routers.deps import get_current_admin, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# PATCH 白名单：可写非敏感键 + scheduler.* 前缀（§9.1 / §3.3）
_WHITELIST_EXACT = {
    "quark_quota_gb",
    "max_episode_size_gb",
    "max_movie_size_gb",
    "scan_interval_minutes",
    "nastools_sync_cooldown_minutes",
    "episode_state_timeout_hours",
    "scheduler_enabled",
    "capacity_safety_margin_gb",
}


def _is_allowed_key(key: str) -> bool:
    return key in _WHITELIST_EXACT or key.startswith("scheduler.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- services 凭据「是否已配置」探测（只回布尔，绝不回显值）----
def _services_configured() -> dict[str, bool]:
    s = app_settings
    return {
        "tmdb": bool((s.TMDB_API_KEY or "").strip()),
        "emby": bool((s.EMBY_BASE_URL or "").strip() and (s.EMBY_API_KEY or "").strip()),
        "cloudsaver": bool(
            (s.CLOUDSAVER_BASE_URL or "").strip()
            and (s.CLOUDSAVER_USERNAME or "").strip()
        ),
        "alist": bool((s.ALIST_BASE_URL or "").strip() and (s.ALIST_TOKEN or "").strip()),
        "aria2": bool((s.ARIA2_RPC_URL or "").strip() and (s.ARIA2_TOKEN or "").strip()),
        "nastools": bool((s.NASTOOLS_BASE_URL or "").strip()),
        "pushplus": bool((s.PUSHPLUS_TOKEN or "").strip()),
        "jwt_secret": (s.JWT_SECRET or "") not in ("", "change_me"),
    }


@router.get("")
async def get_settings(
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1）
    session: AsyncSession = Depends(get_session),
) -> dict:
    """系统配置全量 + services 凭据配置状态布尔。"""
    rows = (await session.execute(select(SystemConfig).order_by(SystemConfig.key))).scalars().all()
    config = {r.key: r.value for r in rows}
    return {
        "system_config": config,
        "config": config,  # 前端契约键（SettingsView 读 res.config）
        "services": _services_configured(),
    }


@router.patch("")
async def patch_settings(
    payload: dict[str, Any],
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1）
    session: AsyncSession = Depends(get_session),
) -> dict:
    """白名单键 UPSERT（字符串值存入 system_config；非白名单键 → 422）。"""
    if not payload:
        raise HTTPException(status_code=422, detail="内容为空")
    for key in payload:
        if not _is_allowed_key(key):
            raise HTTPException(
                status_code=422, detail=f"键 {key!r} 不在可配置白名单（非敏感键）"
            )

    now = _now()
    for key, value in payload.items():
        if value is None:
            continue
        await session.merge(SystemConfig(key=key, value=str(value), updated_at=now))
    await session.commit()

    # M2（Oracle Gate2）：事件驱动冷切换——配置保存即应用，无需重启。
    # 阶段 4 用户经 settings 页启用定时（scheduler_enabled / scheduler.<job_id>）时，
    # 若 PATCH 后不重新应用 job 开关，调度器不会启动对应 job（_apply_job_switches
    # 仅在 lifespan start() 调用一次）。此处 commit 成功后按需重新落地开关：
    # - 延迟导入 app.scheduler（其 import 会实例化 APScheduler），避免本模块 import
    #   链副作用；_apply_job_switches 幂等可重复调用（resume 对已激活 job、pause 对
    #   已暂停 job 均按 next_run_time 判断后无副作用操作）。
    # - 失败仅告警（配置已保存，restart 兜底），不阻塞 PATCH 返回 ok。
    scheduler_touched = any(
        k == "scheduler_enabled" or k.startswith("scheduler.") for k in payload
    )
    if scheduler_touched:
        try:
            from app.scheduler import _apply_job_switches  # noqa: PLC0415 延迟导入

            await _apply_job_switches()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[settings] 调度开关已保存但应用失败，请重启生效（%s）", exc
            )
    return {"ok": True}