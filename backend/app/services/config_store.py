"""进程内配置缓存（Phase 8 配置入库）。

背景：服务凭据（ALIST_*/CLOUDSAVER_*/ARIA2_*/NASTOOLS_*/EMBY_*/TMDB_*/PUSHPLUS_*
QUARK_* 等）全部入 system_config 表，settings 页面可配置（PATCH 保存）；
进程内缓存保证「保存即生效」（PATCH commit 后 refresh()，无需重启）。
系统仍保留 env fallback（旧部署平滑迁移：system_config 缺失时回退
app.config.settings / .env 默认值）。

API：
    load_from_db(): 从 system_config 全量加载到内存（DB 优先 + settings/env fallback
                    —— fallback 由 get(key, default) 的 default 参数实现）
    get(key, default=None): 同步读取，services 层各处直接调用（进程内 dict 读，
                    不引入 async 侵入；未加载时也安全返回 default）
    refresh(): 在 settings PATCH commit 后调用，重新加载（保存即生效）
    is_loaded(): 是否已完成至少一次加载（settings GET 惰性加载用）

线程/协程安全：单 worker 内嵌（uvicorn --workers 1），模块级 dict 读写原子性足够；
不引入锁。refresh/load 由 async 路由协程调用，get 由 services 同步/异步函数调用，
dict 替换（_cache = {...}）为单条赋值，读取方看到的是完整快照。
"""
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import SystemConfig

logger = logging.getLogger(__name__)

# 模块级进程内缓存：key = system_config.key（snake_case），value = 字符串
_cache: dict[str, str] = {}
_loaded: bool = False

# Phase 8 配置入库：system_config 中不回显值的敏感键（settings GET 用 "***" 占位）。
# 遮蔽原则：
#   - token / password / api_key / folder / secret 类一律敏感（如 alist_token、
#     cloudsaver_password、emby_api_key、quark_default_folder、jwt_secret、
#     init_admin_password、tmdb_api_key、pushplus_token、cloudsaver_username、
#     nastools_username/nastools_password——账号名亦属凭据）；
#   - URL 类键（cloudsaver_base_url / emby_base_url / nastools_base_url / tmdb_proxy）
#     不算敏感可回显（前端表单预填需要）；
#   - 例外「统一处理」：alist_base_url / aria2_rpc_url 为服务内部地址（alist 直链
#     网关 / aria2 RPC 端点），按服务凭据统一遮蔽，避免暴露内部网络拓扑。
_SENSITIVE_KEYS = frozenset({
    "alist_base_url", "alist_token",
    "cloudsaver_username", "cloudsaver_password",
    "aria2_rpc_url", "aria2_token",
    "nastools_username", "nastools_password",
    "emby_api_key",
    "tmdb_api_key",
    "pushplus_token",
    "quark_default_folder",
    "jwt_secret",
    "init_admin_password",
})


async def load_from_db() -> None:
    """启动/惰性加载：从 system_config 全量读取到进程内缓存。

    - 成功：_cache = {key: value}，_loaded = True；
    - 失败（DB 不可用/表不存在等）：仅 logger.warning，_loaded 保持 False，
      调用方经 get(key, default) 继续回退 settings/env 值，不阻断启动。
    """
    global _cache, _loaded
    try:
        async with async_session() as session:
            rows = (await session.execute(select(SystemConfig))).scalars().all()
    except Exception as exc:  # noqa: BLE001  查询失败回退 env，不阻断启动
        logger.warning("config_store 从 system_config 加载失败（回退 settings/env 值）: %s", exc)
        return
    _cache = {r.key: r.value for r in rows}
    _loaded = True
    logger.info("config_store 已加载 %d 项 system_config 配置", len(_cache))


async def refresh() -> None:
    """settings PATCH commit 后调用：重新加载全量配置（保存即生效，无需重启）。

    实现等价于 load_from_db；失败同样仅告警，不阻塞 PATCH 返回。
    """
    await load_from_db()


def get(key: str, default=None) -> str | None:
    """同步读取配置值。

    参数:
        key:     system_config 键名（snake_case，如 "alist_token"）
        default: 回退值（services 层传 app.config.settings.X 即实现 env fallback）
    返回:
        缓存中的字符串值；未加载或键不存在时返回 default。
    """
    return _cache.get(key, default)


def is_loaded() -> bool:
    """是否已完成至少一次成功加载（settings GET 惰性加载判定用）。"""
    return _loaded


def is_sensitive(key: str) -> bool:
    """该 system_config 键是否敏感（settings GET 需用 "***" 占位不回显值）。"""
    return key in _SENSITIVE_KEYS
