from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    DATABASE_URL: str = "postgresql+asyncpg://lumen:change_me@host:5432/lumencloud"
    REDIS_URL: str = "redis://host:6379/0"
    JWT_SECRET: str = "change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168
    APP_NAME: str = "LumenCloud"
    DEBUG: bool = False

settings = Settings()
