"""tmdb_cache.tv_status: TMDB TV 连载状态列

Revision ID: 0010_tmdb_cache_tv_status
Revises: 0009_episode_node_fields
Create Date: 2026-09-07

连载判定 TMDB 优先：Emby 库页 series 条目有 tmdb_id 时用 /3/tv/{id} 的
status 字段判定连载，结果缓存进 tmdb_cache 表（复用 7 天 TTL），避免对
同一剧集反复回源。新增 tv_status 列（String(32) nullable）：
Returning Series / Ended / Canceled / Pilot，movie 或无该字段 → NULL。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_tmdb_cache_tv_status"
down_revision: Optional[str] = "0009_episode_node_fields"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "tmdb_cache",
        sa.Column("tv_status", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tmdb_cache", "tv_status")
