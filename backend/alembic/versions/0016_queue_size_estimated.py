"""Add size_estimated to task_queue / download_queue.

task_queue / download_queue 新增 size_estimated 布尔列：标记 file_size 为
「分享总大小 / 文件数」均摊估算值（cloudSaver share-list 不返回单文件 size，
scan._walk_share 估算后置该标记）。下载完成（轮询 + aria2 回调）用
aria2 totalLength 回填真实 file_size 并清估算标记。

Revision ID: 0016
Revises: 0015_episode_info_cache
Create Date: 2026-09-10
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_queue_size_estimated"
down_revision: Optional[str] = "0015_episode_info_cache"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column("task_queue", sa.Column("size_estimated", sa.Boolean(), nullable=True))
    op.add_column("download_queue", sa.Column("size_estimated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("download_queue", "size_estimated")
    op.drop_column("task_queue", "size_estimated")
