from pathlib import Path
from pydantic_settings import BaseSettings

# .env 在项目根目录，config.py 在 backend/app/ 下，需要向上两层
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"

class Settings(BaseSettings):
    model_config = {"env_file": str(_ENV_FILE), "env_file_encoding": "utf-8", "extra": "ignore"}

    DATABASE_URL: str = "postgresql+asyncpg://lumen:change_me@host:5432/lumencloud"
    REDIS_URL: str = "redis://host:6379/0"
    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168
    APP_NAME: str = "LumenCloud"
    DEBUG: bool = False

settings = Settings()
