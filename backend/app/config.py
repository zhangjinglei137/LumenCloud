"""
LumenCloud 配置中心
- 外部服务凭据来自环境变量（容器 env / Docker Secrets），不落数据库。
- JWT 密钥例外（Phase 8）：不再要求 env 提供——首次启动自动生成强随机值并
  落盘 LUMENCLOUD_DATA_DIR/.jwt_secret（chmod 600），重启读文件，永久有效。
"""
import logging
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings
from typing import Optional

logger = logging.getLogger(__name__)

_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = {
        "env_file": str(_ENV_FILE),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # ---- 应用与数据库 ----
    APP_NAME: str = "LumenCloud"
    DEBUG: bool = False
    LUMENCLOUD_DATA_DIR: str = "data"
    DATABASE_URL: Optional[str] = None  # 空 = 内置 SQLite（/app/data/lumencloud.db）

    # ---- 初始化（首次启动创建管理员）----
    INIT_ADMIN_USERNAME: str = "admin"
    # Phase 8 起弃用：admin 初始密码改为首次启动随机生成（日志打印一次），
    # 此字段仅保留作类型/历史兼容，不再被读取或校验。
    INIT_ADMIN_PASSWORD: str = "change_me"
    # Phase 8 起弃用 env 提供：真实密钥文件化于 <LUMENCLOUD_DATA_DIR>/.jwt_secret，
    # 此字段仅作 load_or_create_jwt_secret 写入失败时的回退值。
    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168

    # ---- 外部服务 ----
    TMDB_API_KEY: str = ""
    TMDB_PROXY: str = ""
    # P2-2 出口代理双模式：TMDB 请求出口代理（http(s)://host:port，如科学上网
    # 代理）。与 TMDB_PROXY（镜像根地址）相互独立可叠加：镜像请求同样可走该出口；
    # 无镜像时配合官方地址（api.themoviedb.org）使用。
    TMDB_HTTP_PROXY: str = ""
    EMBY_BASE_URL: str = ""
    EMBY_API_KEY: str = ""
    CLOUDSAVER_BASE_URL: str = ""
    CLOUDSAVER_USERNAME: str = ""
    CLOUDSAVER_PASSWORD: str = ""
    ALIST_BASE_URL: str = ""
    ALIST_TOKEN: str = ""
    ARIA2_RPC_URL: str = ""
    ARIA2_TOKEN: str = ""
    NASTOOLS_BASE_URL: str = ""
    NASTOOLS_USERNAME: str = ""
    NASTOOLS_PASSWORD: str = ""
    PUSHPLUS_TOKEN: str = ""

    # ---- 容量与调度默认值 ----
    QUARK_QUOTA_GB: float = 10.0
    # 夸克中转目录 folderId（cloudSaver save 的 folderId 参数，转存落盘到 alist /quark 挂载目录）。
    # 阶段 1 实证：folderId 缺失时转存不落盘到 /quark（对象 not found）。
    # 取值 = alist Quark 驱动的 root_folder_id（alist 管理 API /api/admin/storage/list 可探测）。
    QUARK_DEFAULT_FOLDER: str = ""
    DEFAULT_MAX_EPISODE_SIZE_GB: float = 1.5
    DEFAULT_MAX_MOVIE_SIZE_GB: float = 5.0
    SCAN_INTERVAL_MINUTES: int = 60
    NASTOOLS_SYNC_COOLDOWN_MINUTES: int = 30
    EPISODE_STATE_TIMEOUT_HOURS: float = 2.0
    # Emby 防重基线缺失（未收录该剧集）时的巡检行为开关（Phase 8，system_config 可覆盖）：
    # True = 旧行为强防重——基线不可用即本轮跳过（防盲入占中转空间，需部署主动开启）；
    # False = 默认软处理——未收录时按全量模式照常搜索入队（scan 搜索链只收集具体文件
    #         并受大小/数量过滤，「防盲入」顾虑已大幅缓解，用户可下载本地没有的剧集）。
    SCAN_BASELINE_REQUIRED: bool = False


settings = Settings()


# ---------------------------------------------------------------------------
# JWT 密钥文件化（Phase 8）
# ---------------------------------------------------------------------------

def load_or_create_jwt_secret(data_dir: str) -> str:
    """读取或生成 JWT 密钥文件（<data_dir>/.jwt_secret，chmod 600）。

    - 文件已存在 → 读取并 strip 返回（重启永久有效，不重新生成）；
    - 文件不存在 → secrets.token_hex(32) 强随机生成（64 位 hex / 192bit 熵），
      内部自行 mkdir 数据目录后写入、chmod 0o600；
    - 任何 I/O 失败 → warning 日志 + 回退 settings.JWT_SECRET（由启动护栏兜底）。
    """
    path = Path(data_dir) / ".jwt_secret"
    try:
        if path.exists():
            secret = path.read_text(encoding="utf-8").strip()
            if secret:
                return secret
        secret = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret + "\n", encoding="utf-8")
        path.chmod(0o600)
        # 读回确认：并发首启（多进程同时写文件）时保证返回值与落盘一致
        return path.read_text(encoding="utf-8").strip() or secret
    except OSError as exc:
        logger.warning("JWT 密钥文件 %s 读写失败（%s），回退 settings.JWT_SECRET", path, exc)
        return settings.JWT_SECRET


# 模块加载时解析一次：auth 签发与 deps 验签共用同一密钥。import 顺序无关——
# load_or_create_jwt_secret 内部自行 mkdir 数据目录，早于 main.py lifespan。
_JWT_SECRET = load_or_create_jwt_secret(settings.LUMENCLOUD_DATA_DIR)