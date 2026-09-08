"""
数据库引擎与会话
- DATABASE_URL 为空   → 内置 SQLite（WAL 模式，volume 下的 lumencloud.db）
- DATABASE_URL 有值   → 外部数据库（如 postgresql+asyncpg://）
"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from app.config import settings

logger = logging.getLogger(__name__)


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


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """SQLite 连接级 PRAGMA（设计文档 §2.2 / §14 要求）：WAL / 外键 / 忙等待超时"""
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _alembic_config():
    """Alembic Config：script_location 绝对化，与运行 cwd 解耦（供 init_db 使用）"""
    from alembic.config import Config

    alembic_dir = Path(__file__).resolve().parent.parent / "alembic"
    cfg = Config(str(alembic_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(alembic_dir))
    return cfg


async def init_db() -> None:
    """启动时执行 Alembic 迁移至 head（建表权威来源 = versions/ 手写迁移脚本）。

    替代骨架阶段的 create_all；Alembic 自身使用同步 engine（见 alembic/env.py）。

    AUTO_MIGRATE（安全加固）：False → 跳过自动迁移，仅告警提示手动执行
    `alembic upgrade head`。生产可选 AUTO_MIGRATE=0 配合手动迁移窗口
    （多副本/需人工把控迁移时机的部署），默认 True 保持原行为。
    """
    if not settings.AUTO_MIGRATE:
        logger.warning(
            "AUTO_MIGRATE=False：跳过自动 Alembic 迁移，请手动执行 "
            "`alembic upgrade head` 后重启服务"
        )
        return

    from alembic import command

    from app import models  # noqa: F401  确保模型已注册（供 autogenerate 参考）

    cfg = _alembic_config()
    await asyncio.to_thread(command.upgrade, cfg, "head")
