"""add transfer_queue.save_task_id: cloudSaver 转存任务 id（save 幂等标记）

Revision ID: 0002_save_task_id
Revises: 0001_initial_schema
Create Date: 2026-09-06

P2-10（council）：cloudsaver.save 受理即落 task_id，get_link/add_uri 后续失败
重试时不再重复 save 同一文件（防重复转存占空间 / cloudSaver 端重复任务）。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_save_task_id"
down_revision: Optional[str] = "0001_initial_schema"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "transfer_queue",
        sa.Column("save_task_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transfer_queue", "save_task_id")
