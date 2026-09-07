"""episode_state 节点字段：五节点任务模型数据层

Revision ID: 0009_episode_node_fields
Revises: 0008_media_series_status
Create Date: 2026-09-07

五节点任务模型（oracle 决策）在 episode_state 上并行引入节点级状态字段：
- node：当前节点（idle/transfer/download/downloading/scrape/library/failed/done）
- node_attempt：节点级重试计数（默认 0）
- node_started_at / node_finished_at：节点起止时间（可空）
- node_error：节点级失败诊断（可空）

数据迁移：旧 state 映射到 node（queued→idle、transferring→transfer、
downloading→downloading、done→done、failed→failed、其它→idle），仅更新
node='idle' 的行（幂等，可安全重复执行，不覆盖已有节点值）。

双后端兼容：server_default 用常量（'idle'/0），SQLite 与 Postgres 的
ADD COLUMN 均原生支持；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_episode_node_fields"
down_revision: Optional[str] = "0008_media_series_status"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "episode_state",
        sa.Column(
            "node", sa.Text(), nullable=False, server_default=sa.text("'idle'")
        ),
    )
    op.add_column(
        "episode_state",
        sa.Column("node_attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "episode_state",
        sa.Column("node_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "episode_state",
        sa.Column("node_finished_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "episode_state",
        sa.Column("node_error", sa.Text(), nullable=True),
    )

    # 数据迁移：旧 state → node（仅处理 node 仍为默认 'idle' 的行，幂等）
    op.execute(
        """
        UPDATE episode_state
        SET node = CASE state
            WHEN 'queued' THEN 'idle'
            WHEN 'transferring' THEN 'transfer'
            WHEN 'downloading' THEN 'downloading'
            WHEN 'done' THEN 'done'
            WHEN 'failed' THEN 'failed'
            ELSE 'idle'
        END
        WHERE node = 'idle'
        """
    )


def downgrade() -> None:
    op.drop_column("episode_state", "node_error")
    op.drop_column("episode_state", "node_finished_at")
    op.drop_column("episode_state", "node_started_at")
    op.drop_column("episode_state", "node_attempt")
    op.drop_column("episode_state", "node")
