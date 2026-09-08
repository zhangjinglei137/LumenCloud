"""download_queue.wait_since + 准入锁行预置（议会验证 P1 修复）

Revision ID: 0013_wait_since_admission_lock
Revises: 0012_task_download_queue
Create Date: 2026-09-08

- download_queue 增加 wait_since 列：quota_wait 状态进入时间（容量不足时置
  quota_wait+wait_since，容量释放回 pending；持续 >1 天触发送时告警）。
- 预置 system_config 锁行 `_transfer_admission_lock`：transfer._try_admit_one 的
  PG 多 worker 准入段用 `SELECT ... FOR UPDATE` 对该行上锁串行化（SQLite 单写者
  天然无此需要；PG 部署无锁行则 FOR UPDATE 不阻塞 → 多 worker TOCTOU）。
"""
from datetime import datetime, timezone
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_wait_since_admission_lock"
down_revision: Optional[str] = "0012_task_download_queue"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None

_ADMISSION_LOCK_KEY = "_transfer_admission_lock"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upgrade() -> None:
    op.add_column(
        "download_queue",
        sa.Column("wait_since", sa.DateTime(), nullable=True),
    )
    # 准入锁行预置（幂等：UNIQUE/主键冲突时忽略——用 ON CONFLICT 语义，SQLite/PG 通用写法）
    op.execute(
        sa.text(
            "INSERT INTO system_config (key, value, updated_at) "
            "VALUES (:key, :value, :updated_at) "
            "ON CONFLICT (key) DO NOTHING"
        ).bindparams(
            key=_ADMISSION_LOCK_KEY, value="", updated_at=_now(),
        )
    )


def downgrade() -> None:
    op.drop_column("download_queue", "wait_since")
    op.execute(
        sa.text("DELETE FROM system_config WHERE key = :nk").bindparams(
            nk=_ADMISSION_LOCK_KEY
        )
    )