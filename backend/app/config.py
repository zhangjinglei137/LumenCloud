"""
LumenCloud 配置中心
所有敏感凭据一律来自环境变量（容器 env / Docker Secrets），不落数据库。
"""
from pathlib import Path
from pydantic_settings import BaseSettings
from typing import Optional

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
    INIT_ADMIN_PASSWORD: str = "change_me"
    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168

    # ---- 外部服务 ----
    TMDB_API_KEY: str = ""
    TMDB_PROXY: str = ""
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


settings = Settings()