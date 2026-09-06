"""add tmdb_cache 表：TMDB 元数据缓存

Revision ID: 0004_tmdb_cache
Revises: 0003_media_poster_path
Create Date: 2026-09-06

新增 tmdb_cache 缓存表：以字符串 TMDB id 为主键，缓存新增影视/想看时的
title/media_type/poster_path/year 元数据，减少对 TMDB API 的重复调用。
读写逻辑在 services/tmdb.py（并行开发中），本迁移只建表结构。

双后端兼容：tmdb_id 为 String(32) 主键，Postgres/SQLite 均原生支持；
无外键、无唯一索引依赖，create_table 直建即可。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，本 id 14 字符（0003 教训）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tmdb_cache"
down_revision: Optional[str] = "0003_media_poster_path"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.create_table(
        "tmdb_cache",
        sa.Column("tmdb_id", sa.String(32), primary_key=True),  # 字符串 TMDB id
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("media_type", sa.String(32), nullable=False),  # movie / tv
        sa.Column("poster_path", sa.Text(), nullable=True),  # TMDB 图床相对路径
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("tmdb_cache")
