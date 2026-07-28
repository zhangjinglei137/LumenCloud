from urllib.parse import urlparse, unquote
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import settings


def _build_url(url_str: str) -> URL:
    """解析 DATABASE_URL 并显式解码特殊字符，绕过 SQLAlchemy URL 编码问题"""
    parsed = urlparse(url_str)
    return URL.create(
        "postgresql+asyncpg",
        username=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
    )

engine = create_async_engine(_build_url(settings.DATABASE_URL), echo=settings.DEBUG)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
