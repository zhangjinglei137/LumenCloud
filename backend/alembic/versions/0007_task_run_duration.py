"""task_run.duration_seconds: 任务真实耗时（秒，job 入口 time.monotonic() 计时）

Revision ID: 0007_task_run_duration
Revises: 0006_tmdb_id_unique
Create Date: 2026-09-06

Q8①（P2）：record_task_run 此前 started_at 与 finished_at 同时取当前时间，前端
LogsView 用二者差值算耗时恒为 0。新增 duration_seconds 列，由各 job 入口以
time.monotonic() 真实计时后传入（record_task_run 可选参数）；历史记录为 None。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_task_run_duration"
down_revision: Optional[str] = "0006_tmdb_id_unique"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "task_run",
        sa.Column("duration_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_run", "duration_seconds")
