"""task_queue / download_queue: 影视下载两队列建表（影视下载两队列重设计 §3）

Revision ID: 0012_task_download_queue
Revises: 0011_task_run_scan_fields
Create Date: 2026-09-07

两队列取代旧三表（episode_state / transfer_queue / download_task）：
- task_queue      探测层：缺失集 + 探测结果快照（pending/probing/ready/unmatched/error/done）
- download_queue  执行层：转存→下载→刮削→入库 状态机 + 容量预留记账
  （pending/transferring/downloading/scrape/library/quota_wait/done/skipped/failed）

建表与 backend/app/models/__init__.py 一一对应（手写，非 autogenerate）。
旧三表本轮不动（数据迁移脚本 scripts/ 另出，冷切换时执行，见设计文档 §9）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_task_download_queue"
down_revision: Optional[str] = "0011_task_run_scan_fields"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None

# 双后端主键类型（与 backend/app/models/__init__.py 的 BIG_PK 一致）
_BIG_ID = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    # ---- task_queue 任务队列（探测层）----
    op.create_table(
        "task_queue",
        sa.Column("id", _BIG_ID, sa.Identity(), nullable=False),
        sa.Column("media_id", sa.BigInteger(), nullable=False),
        sa.Column("episode", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("share_code", sa.Text(), nullable=True),
        sa.Column("pwd_id", sa.Text(), nullable=True),
        sa.Column("stoken", sa.Text(), nullable=True),
        sa.Column("receive_code", sa.Text(), nullable=True),
        sa.Column("fids", sa.Text(), nullable=True),
        sa.Column("fid_tokens", sa.Text(), nullable=True),
        sa.Column("folder_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("probe_attempt", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("silent_until", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_id", "episode", name="uq_task_queue_media_episode"),
    )
    op.create_index("idx_tqk_status", "task_queue", ["status"])
    op.create_index("idx_tqk_media", "task_queue", ["media_id"])
    op.create_index("idx_tqk_silent_until", "task_queue", ["silent_until"])

    # ---- download_queue 下载队列（执行层）----
    op.create_table(
        "download_queue",
        sa.Column("id", _BIG_ID, sa.Identity(), nullable=False),
        sa.Column("media_id", sa.BigInteger(), nullable=False),
        sa.Column("episode", sa.Text(), nullable=False),
        sa.Column("task_queue_id", sa.BigInteger(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("share_code", sa.Text(), nullable=False),
        sa.Column("pwd_id", sa.Text(), nullable=True),
        sa.Column("stoken", sa.Text(), nullable=True),
        sa.Column("receive_code", sa.Text(), nullable=True),
        sa.Column("fids", sa.Text(), nullable=True),
        sa.Column("fid_tokens", sa.Text(), nullable=True),
        sa.Column("folder_id", sa.Text(), nullable=True),
        sa.Column("download_name", sa.Text(), nullable=True),
        sa.Column("aria2_gid", sa.Text(), nullable=True),
        sa.Column("quark_path", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("save_task_id", sa.Text(), nullable=True),
        sa.Column("save_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("node_attempt", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("node_started_at", sa.DateTime(), nullable=True),
        sa.Column("node_finished_at", sa.DateTime(), nullable=True),
        sa.Column("node_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("quota_reject_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.ForeignKeyConstraint(["media_id"], ["media.id"]),
        sa.ForeignKeyConstraint(["task_queue_id"], ["task_queue.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("media_id", "episode", name="uq_download_queue_media_episode"),
    )
    op.create_index("idx_dqk_status", "download_queue", ["status"])
    op.create_index("idx_dqk_media", "download_queue", ["media_id"])


def downgrade() -> None:
    op.drop_index("idx_dqk_media", table_name="download_queue")
    op.drop_index("idx_dqk_status", table_name="download_queue")
    op.drop_table("download_queue")
    op.drop_index("idx_tqk_silent_until", table_name="task_queue")
    op.drop_index("idx_tqk_media", table_name="task_queue")
    op.drop_index("idx_tqk_status", table_name="task_queue")
    op.drop_table("task_queue")
