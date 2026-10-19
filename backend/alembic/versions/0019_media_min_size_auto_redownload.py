"""media 最小大小覆盖列 + download_queue 自动重下计数列

Revision ID: 0019_media_min_size_auto_redownload
Revises: 0018_user_token_version
Create Date: 2026-10-19

背景（change media-min-size-and-emby-redownload）：
- media 新增 min_episode_size_gb / min_movie_size_gb 可空列（NULL/0 = 不限制），
  与既有 max_* 同构；
- download_queue 新增 auto_redownload_count（非空默认 0）：Emby 侧删除后自动
  重下的累计次数，人工 retry 时清零。

双后端兼容：与 0016/0018 同为带 server_default 的 ADD COLUMN（现有行自动填 0）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

# ⚠ revision id ≤32 字符（alembic_version.version_num 在 Postgres 为 VARCHAR(32)）。
# 任务书给出的 "0019_media_min_size_auto_redownload" 为 35 字符，故取短 id。
revision: str = "0019_media_min_size"
down_revision: Optional[str] = "0018_user_token_version"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column("media", sa.Column("min_episode_size_gb", sa.Float(), nullable=True))
    op.add_column("media", sa.Column("min_movie_size_gb", sa.Float(), nullable=True))
    op.add_column(
        "download_queue",
        sa.Column(
            "auto_redownload_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("download_queue", "auto_redownload_count")
    op.drop_column("media", "min_movie_size_gb")
    op.drop_column("media", "min_episode_size_gb")
