"""task_run.phases / scan_detail: 巡检可见性改造（5 阶段进度 + 结果摘要）

Revision ID: 0011_task_run_scan_fields
Revises: 0010_tmdb_cache_tv_status
Create Date: 2026-09-07

巡检从「结束后插一条 task_run」改为「触发即插 running 记录、结束后 UPDATE 同一条
终态」（app/tasks/scan.py 的 _create_scan_run / _finish_scan_run）。为支撑前端
「信息列人话化 + 5 阶段进度 + 结果摘要」展示，task_run 新增两列：

- phases:      TEXT NULL —— 巡检 5 阶段 JSON（check/search/match/enqueue/finish，
               每阶段 {status, started_at, finished_at}，naive UTC ISO 字符串）
- scan_detail: TEXT NULL —— 结果摘要 JSON（missing_total/enqueued/existing_skipped/
               size_filtered/unmatched/non_video/failed_phase/missing_items）

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_task_run_scan_fields"
down_revision: Optional[str] = "0010_tmdb_cache_tv_status"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "task_run",
        sa.Column("phases", sa.Text(), nullable=True),
    )
    op.add_column(
        "task_run",
        sa.Column("scan_detail", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("task_run", "scan_detail")
    op.drop_column("task_run", "phases")
