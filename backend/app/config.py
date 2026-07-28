from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    POSTGRES_USER: str = "lumen"
    POSTGRES_PASSWORD: str = "change_me"
    POSTGRES_DB: str = "lumencloud"
    DATABASE_URL: str = "postgresql+asyncpg://lumen:change_me@192.168.3.31:5432/lumencloud"

    REDIS_URL: str = "redis://192.168.3.31:6379/0"

    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168

    TMDB_API_KEY: str = ""
    EMBY_BASE_URL: str = "http://192.168.3.31:8096"
    EMBY_API_KEY: str = ""
    CLOUDSAVER_BASE_URL: str = "http://192.168.3.31:8008"
    CLOUDSAVER_USERNAME: str = "admin"
    CLOUDSAVER_PASSWORD: str = ""
    ARIA2_RPC_URL: str = "http://192.168.3.31:6800/jsonrpc"
    ARIA2_SECRET: str = ""
    ARIA2_DOWNLOAD_DIR: str = "/downloads"
    NASTOOLS_BASE_URL: str = "http://192.168.3.31:3000"
    NASTOOLS_USERNAME: str = "admin"
    NASTOOLS_PASSWORD: str = ""
    ALIST_BASE_URL: str = "http://192.168.3.31:5244"
    ALIST_TOKEN: str = ""
    PUSHPLUS_TOKEN: str = ""
    APP_NAME: str = "LumenCloud"
    DEBUG: bool = False

settings = Settings()
