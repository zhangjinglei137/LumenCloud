"""设置 API（admin，docs/新系统设计.md §9.1 配置接口契约）。

- GET  /api/settings  system_config 全量 + services 凭据「是否已配置」布尔 +
                      editable_keys 前端可配置键清单（Phase 8）
                      —— 仅 jwt_secret / init_admin_password 以 "***" 占位隐藏；
                      其余服务凭据键明文回显（Q5：地址/令牌/用户/密码/token 全部明文）
- PATCH /api/settings 白名单键 UPSERT（Phase 8 起含服务凭据键）；commit 后刷新
                      进程内配置缓存（config_store.refresh，保存即生效）；含调度
                      相关键时事件驱动重新应用 job 开关（M2，Oracle Gate2）
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_settings
from app.models import SystemConfig, User
from app.routers.deps import get_current_admin, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

# PATCH 白名单：可写键（§9.1 / §3.3）。含 scheduler.* 前缀动态键 + 服务凭据键。
#
# Phase 8 配置入库：服务凭据全部入 system_config（settings 页面可配置，PATCH
# 保存即生效），键名一律 snake_case（= system_config / config_store 键名）。
_WHITELIST_EXACT = {
    "quark_quota_gb",
    "max_episode_size_gb",
    "max_movie_size_gb",
    "nastools_sync_cooldown_minutes",
    "episode_state_timeout_hours",
    # 影视下载两队列重设计 §4.2：scrape/library 节点独立超时回退阈值（小时）
    "scrape_revert_timeout_hours",
    "library_revert_timeout_hours",
    # 影视下载两队列重设计：aria2 / NaSTools hook 回调鉴权密钥（settings 页可配）
    "internal_aria2_webhook_secret",
    "internal_nastools_webhook_token",
    "scheduler_enabled",
    "capacity_safety_margin_gb",
    # Emby 防重基线缺失（未收录该剧集）时的巡检行为开关（默认关 = 照常搜索下载）
    "scan_baseline_required",
    # 阶段 4 生产化 / E：容量使用率告警阈值（交付 1，默认 0.90 在代码常量）
    "capacity_alert_threshold",
    # ---- Phase 8 配置入库：服务凭据可写键 ----
    "alist_base_url", "alist_token",
    "cloudsaver_base_url", "cloudsaver_username", "cloudsaver_password",
    "aria2_rpc_url", "aria2_token",
    "nastools_base_url", "nastools_username", "nastools_password",
    "emby_base_url", "emby_api_key",
    "tmdb_api_key", "tmdb_proxy", "tmdb_http_proxy", "tmdb_poster_proxy",
    "pushplus_token",
    "quark_default_folder",
    # Q2 后端：剧集页可见媒体库白名单（Emby VirtualFolder ItemId，逗号分隔，
    # 如 "a1b2c3,d4e5f6"；空串 = 不启用白名单过滤）。PATCH 可写，但属「业务
    # 参数」——不进 editable_keys（见 _EDITABLE_KEYS 注释），避免前端误渲染为
    # 服务凭据文本框。
    "emby_series_library_ids",
}

# Phase 8 配置入库：前端可配置键清单（= config_store 可管理键，scheduler.* 前缀
# 键为动态不可枚举，不在此列；GET /api/settings 以 editable_keys 字段返回给前端渲染表单）。
# Q2：emby_series_library_ids 为业务行为参数（设置页以下拉多选渲染在「业务参数」区），
# 必须从 editable_keys 排除——若进入该清单，前端会把此键当作服务凭据字段渲染成文本框。
_EDITABLE_KEYS = frozenset(_WHITELIST_EXACT - {"emby_series_library_ids"})

# 已废弃设置键：system_config 存量保留，但 GET 响应不再透传（避免前端
# fallback 展示英文键名）。PATCH 白名单（_WHITELIST_EXACT）本就不含这些键。
_RETIRED_EXACT = frozenset({"download_queue_max_concurrent", "scan_interval_minutes"})


def _is_allowed_key(key: str) -> bool:
    return key in _WHITELIST_EXACT or key.startswith("scheduler.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- services 凭据「是否已配置」探测（只回布尔，绝不回显值）----
# Phase 8 配置入库：凭据可能来自 DB（system_config / config_store）或 env，
# 判定不能只看 env —— 统一按 config_store.get(key, settings.X)（DB 优先 + env
# fallback）非空即视为已配置。
def _services_configured() -> dict[str, bool]:
    from app.services import config_store  # noqa: PLC0415 延迟导入

    s = app_settings

    def _get(key: str, default: str) -> str:
        return (config_store.get(key, default) or "").strip()

    return {
        "tmdb": bool(_get("tmdb_api_key", s.TMDB_API_KEY)),
        "emby": bool(
            _get("emby_base_url", s.EMBY_BASE_URL) and _get("emby_api_key", s.EMBY_API_KEY)
        ),
        "cloudsaver": bool(
            _get("cloudsaver_base_url", s.CLOUDSAVER_BASE_URL)
            and _get("cloudsaver_username", s.CLOUDSAVER_USERNAME)
        ),
        "alist": bool(_get("alist_base_url", s.ALIST_BASE_URL) and _get("alist_token", s.ALIST_TOKEN)),
        "aria2": bool(_get("aria2_rpc_url", s.ARIA2_RPC_URL) and _get("aria2_token", s.ARIA2_TOKEN)),
        "nastools": bool(_get("nastools_base_url", s.NASTOOLS_BASE_URL)),
        "pushplus": bool(_get("pushplus_token", s.PUSHPLUS_TOKEN)),
        # Q5：jwt_secret 不存 DB——config.py 首启自动生成 <data_dir>/.jwt_secret（chmod 600）。
        # 按密钥文件是否已落盘判定（load_or_create 保证文件存在即有效随机密钥），
        # 避免 DB 无键 fallback "change_me" 导致永远误报未配置。
        "jwt_secret": (Path(s.LUMENCLOUD_DATA_DIR) / ".jwt_secret").exists(),
    }


@router.get("")
async def get_settings(
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1）
    session: AsyncSession = Depends(get_session),
) -> dict:
    """系统配置全量 + services 凭据配置状态布尔 + 前端可配置键清单。

    Phase 8 配置入库：
    - 首次 GET 惰性加载进程内配置缓存（config_store），services 判定需 DB 值；
    - system_config 中敏感键（config_store._SENSITIVE_KEYS：仅 jwt_secret 与
      init_admin_password，Q5 收紧）不回显值，以 "***" 占位；其余服务凭据键
      （token/password/api_key 等）明文回显，前端表单直接预填可编辑；
    - 新增 editable_keys 字段：前端可配置的键清单（= config_store 可管理键）。
    """
    from app.services import config_store  # noqa: PLC0415 延迟导入

    if not config_store.is_loaded():
        await config_store.load_from_db()

    rows = (await session.execute(select(SystemConfig).order_by(SystemConfig.key))).scalars().all()
    config: dict[str, str] = {}
    for r in rows:
        if r.key in _RETIRED_EXACT:
            continue  # 已废弃键不透传（存量保留，不删）
        if config_store.is_sensitive(r.key):
            config[r.key] = "***"  # 敏感键不回显值（占位）
        else:
            config[r.key] = r.value
    return {
        "system_config": config,
        "config": config,  # 前端契约键（SettingsView 读 res.config）
        "services": _services_configured(),
        "editable_keys": sorted(_EDITABLE_KEYS),  # Phase 8：前端可配置键清单
    }


@router.patch("")
async def patch_settings(
    payload: dict[str, Any],
    admin: User = Depends(get_current_admin),  # 仅 admin（§9.1）
    session: AsyncSession = Depends(get_session),
) -> dict:
    """白名单键 UPSERT（字符串值存入 system_config；非白名单键 → 422）。

    Phase 8 配置入库：commit 后刷新进程内配置缓存，凭据 PATCH 保存即生效。
    """
    if not payload:
        raise HTTPException(status_code=422, detail="内容为空")
    for key in payload:
        if not _is_allowed_key(key):
            raise HTTPException(
                status_code=422, detail=f"键 {key!r} 不在可配置白名单"
            )

    now = _now()
    for key, value in payload.items():
        if value is None:
            continue
        await session.merge(SystemConfig(key=key, value=str(value), updated_at=now))
    await session.commit()

    # Phase 8 配置入库：保存即生效——commit 成功后刷新进程内配置缓存，无需重启。
    # 延迟导入 app.services.config_store；失败仅告警（配置已持久化，restart 兜底），
    # 不阻塞 PATCH 返回 ok。
    try:
        from app.services import config_store  # noqa: PLC0415 延迟导入

        await config_store.refresh()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[settings] 配置缓存刷新失败（DB 已保存，重启兜底）: %s", exc)

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


@router.post("/verify-quark")
async def verify_quark(admin: User = Depends(get_current_admin)) -> dict:
    """设置页「验证 folderId」：探测 alist /quark 挂载与 Quark 驱动 root_folder_id，
    与 quark_default_folder 比对，返回结构化诊断（内部捕获全部失败，不抛异常）。"""
    from app.services import alist  # noqa: PLC0415 延迟导入（与现有延迟导入风格一致）

    return await alist.diagnose_quark_mount()