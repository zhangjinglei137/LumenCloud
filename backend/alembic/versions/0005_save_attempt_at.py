"""add transfer_queue.save_attempt_at: cloudSaver save 受理时间（超时兜底）

Revision ID: 0005_save_attempt_at
Revises: 0004_tmdb_cache
Create Date: 2026-09-06

P0-1（council）：save_task_id 幂等标记存在但对应 save 受理时间过久时强制重新
save（_SAVE_ATTEMPT_MAX_SECONDS 兜底），杜绝「已受理未落盘」导致的盲等死循环。
本列仅用于重试时判断 save 受理时间，save 一受理即随 save_task_id 一起落库。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_save_attempt_at"
down_revision: Optional[str] = "0004_tmdb_cache"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "transfer_queue",
        sa.Column("save_attempt_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transfer_queue", "save_attempt_at")
