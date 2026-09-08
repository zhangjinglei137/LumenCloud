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
import asyncio
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import SystemConfig

logger = logging.getLogger(__name__)

# 模块级进程内缓存：key = system_config.key（snake_case），value = 字符串
_cache: dict[str, str] = {}
_loaded: bool = False

# Phase 8 配置入库：system_config 中不回显值的敏感键（settings GET 用 "***" 占位）。
# 遮蔽原则（Q5 收紧为仅系统内部密钥遮蔽）：
#   - 仅 jwt_secret（JWT 签名密钥，运行期文件落盘）与 init_admin_password
#     （初始管理员密码，仅首启预置）隐藏——二者为系统内部密钥，不在可编辑
#     白名单表单中，保持 "***" 占位；
#   - 服务凭据键（alist_token / cloudsaver_password / aria2_token / nastools_password /
#     emby_api_key / tmdb_api_key / pushplus_token）、内部服务地址（alist_base_url /
#     aria2_rpc_url）与默认目录（quark_default_folder）一律明文回显——settings GET
#     返回真实值，前端表单直接预填可编辑（Q5 用户确认：地址/令牌/用户/密码/token
#     全部明文显示，仅 jwt_secret 与 init_admin_password 隐藏）；
#   - username / URL 类键本就不在此列，始终明文回显。
_SENSITIVE_KEYS = frozenset({
    "jwt_secret",
    "init_admin_password",
})


# 启动自愈（安全加固）：load_from_db 失败重试次数与退避起始间隔（0.2s/0.4s/0.8s）。
# DB 容器比本进程起步慢的部署，重试窗口内恢复即加载成功，避免启动期误告警。
_CONFIG_LOAD_RETRIES = 3
_CONFIG_LOAD_RETRY_DELAY = 0.2


async def load_from_db() -> None:
    """启动/惰性加载：从 system_config 全量读取到进程内缓存。

    - 成功：_cache = {key: value}，_loaded = True；
    - 失败：指数退避重试（0.2s/0.4s/0.8s，共 _CONFIG_LOAD_RETRIES 次）仍失败
      才 logger.warning，_loaded 保持 False，调用方经 get(key, default) 继续
      回退 settings/env 值，不阻断启动。
    """
    global _cache, _loaded
    delay = _CONFIG_LOAD_RETRY_DELAY
    for attempt in range(1, _CONFIG_LOAD_RETRIES + 1):
        try:
            async with async_session() as session:
                rows = (await session.execute(select(SystemConfig))).scalars().all()
            _cache = {r.key: r.value for r in rows}
            _loaded = True
            logger.info("config_store 已加载 %d 项 system_config 配置", len(_cache))
            return
        except Exception as exc:  # noqa: BLE001  查询失败重试，最终回退 env，不阻断启动
            if attempt < _CONFIG_LOAD_RETRIES:
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.warning(
                    "config_store 从 system_config 加载失败（重试 %d 次后回退 "
                    "settings/env 值）: %s",
                    _CONFIG_LOAD_RETRIES,
                    exc,
                )


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
