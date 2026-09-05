"""LumenCloud Alembic 迁移环境。

要点：
- 使用**同步 engine**执行 DDL（Alembic 不支持异步驱动）：
    DATABASE_URL 为空            → sqlite:///<LUMENCLOUD_DATA_DIR>/lumencloud.db
    sqlite+aiosqlite://...       → 转 sqlite://...
    postgresql+asyncpg://...     → 转 postgresql://...
- 同时支持 offline（`alembic upgrade head --sql`）与 online 两种模式；
- SQLite 下开启 render_as_batch（ALTER 走 batch 模式，支持 SQLite 的表重建迁移）；
- 建表 DDL 权威来源为 backend/alembic/versions/ 手写迁移脚本；
  autogenerate（`alembic revision --autogenerate`）仅作字段比对的辅助参考，
  产物需人工复核后再提交，不直接作为上线依据。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine

from app.config import settings
from app import models  # noqa: F401  注册全部模型，保证 target_metadata 完整
from app.database import Base

# 命令行运行时（cwd ≠ backend 时）保证能 import app.*（prepend_sys_path 之外的兜底）
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sqlite_file_url(prefix: str, path: str) -> str:
    """把 sqlite:/// 之后的路径归一化为绝对路径，避免 cwd 依赖。"""
    if Path(path).is_absolute():
        return f"{prefix}{path}"
    return f"{prefix}{Path(path).resolve()}"


def _sync_url() -> str:
    """依据 settings.DATABASE_URL 推导同步驱动 URL。

    env.py 全程用同步 engine（alembic 同步驱动），URL 需由 async 形式转换。
    """
    url = settings.DATABASE_URL
    if not url:
        # 与 app.database._sqlite_url 对齐：空 → 内置 SQLite
        data_dir = Path(settings.LUMENCLOUD_DATA_DIR).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{data_dir / 'lumencloud.db'}"
    if url.startswith("sqlite:///"):
        return _sqlite_file_url("sqlite:///", url[len("sqlite:///"):])
    if url.startswith("sqlite+aiosqlite:///"):
        return _sqlite_file_url("sqlite:///", url[len("sqlite+aiosqlite:///"):])
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg", "postgresql", 1)
    return url


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """offline 模式：只生成 SQL 脚本，不连数据库。"""
    url = _sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """online 模式：同步 engine 直连执行。

    注：Postgres 在线迁移需同步驱动（psycopg2 等）；当前 requirements 未内置，
    SQLite 在线迁移开箱即用，Postgres 请先 pip install psycopg2-binary。
    """
    engine = create_engine(_sync_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=engine.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
