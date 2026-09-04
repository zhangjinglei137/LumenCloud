"""
数据库引擎与会话
- DATABASE_URL 为空   → 内置 SQLite（WAL 模式，volume 下的 lumencloud.db）
- DATABASE_URL 有值   → 外部数据库（如 postgresql+asyncpg://）
"""
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import settings


class Base(DeclarativeBase):
    pass


def _sqlite_url() -> str:
    data_dir = Path(settings.LUMENCLOUD_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{data_dir / 'lumencloud.db'}"


def _engine():
    url = settings.DATABASE_URL or _sqlite_url()
    if url.startswith("sqlite"):
        # WAL + 单进程（uvicorn --workers 1）——避免多进程 SQLite 锁冲突
        return create_async_engine(
            url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_async_engine(url, pool_pre_ping=True)


engine = _engine()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """启动时建表（骨架阶段使用 create_all，后续引入 Alembic 迁移）"""
    from app import models  # noqa: F401  确保模型已注册

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)