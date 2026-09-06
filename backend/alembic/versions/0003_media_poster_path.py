"""add media.poster_path: 影视海报相对路径（TMDB 图床 /t/p/w500/...）

Revision ID: 0003_media_poster_path
Revises: 0002_save_task_id
Create Date: 2026-09-06

线上反馈修复 Q2：MediaAddView 从 TMDB 选中影视（含 poster_path）后 POST /api/media
只传 title/tmdb_id/media_type，Media 表无 poster_path 列 → 添加后影视库海报丢失。
本迁移为 media 表补 poster_path 列（Text, nullable=True，仅存相对路径）。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（0002 教训，本 id 21 字符）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_media_poster_path"
down_revision: Optional[str] = "0002_save_task_id"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("poster_path", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media", "poster_path")