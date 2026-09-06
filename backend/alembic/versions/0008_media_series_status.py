"""media.series_status: Emby 连载状态列（在更/完结）

Revision ID: 0008_media_series_status
Revises: 0007_task_run_duration
Create Date: 2026-09-06

Q12（P2）：Emby 库页「仅在更」筛选已有（list_library 的 status 参数请求
SeriesStatus），但列表数据不回传连载状态，Media 表也无处落库。新增
series_status 列（Text nullable）：Emby Series 条目为 "continuing"/"ended"，
NULL=未知或电影条目。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_media_series_status"
down_revision: Optional[str] = "0007_task_run_duration"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("series_status", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media", "series_status")