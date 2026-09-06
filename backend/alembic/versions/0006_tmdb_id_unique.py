"""media.tmdb_id 加 UNIQUE 约束（应用于去重的 DB 层兜底）

Revision ID: 0006_tmdb_id_unique
Revises: 0005_save_attempt_at
Create Date: 2026-09-06

Q2①（P1）：POST /api/approvals、POST /api/approvals/{id}/approve、POST /api/media
三入口写入前均已做应用层查重（tmdb_id 非空且已存在于 media 表 → 409），本迁移为
DB 层兜底：media.tmdb_id 加 UNIQUE 约束。保持 nullable——SQLite/PostgreSQL 的
UNIQUE 均允许多个 NULL，不影响 tmdb_id 为空的影视。

在加约束前先幂等清理历史重复：对 tmdb_id 非空的重复组仅保留最早一条（每组取
id 最小者，即最早入库），删除其余；DELETE 用 SQLite 与 PostgreSQL 通用写法。

未改动任何历史迁移文件；约束命名 uq_media_tmdb_id 与 models 的 unique=True
（autogenerate 会映射为相同语义）对齐。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_tmdb_id_unique"
down_revision: Optional[str] = "0005_save_attempt_at"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None

# SQLite/PostgreSQL 通用：删除 tmdb_id 非空的重复行，每组仅保留 id 最小（最早）一条。
# NOT IN 子查询仅含合法 MIN(id)，无 NULL 风险；tmdb_id 为空的行不受影响。
_DEDUPE_SQL = (
    "DELETE FROM media WHERE id NOT IN "
    "(SELECT MIN(id) FROM media WHERE tmdb_id IS NOT NULL GROUP BY tmdb_id)"
)


def upgrade() -> None:
    # 1) 先清理重复（幂等：无重复时删除 0 行，可重复执行）
    op.execute(sa.text(_DEDUPE_SQL))
    # 2) UNIQUE 约束兜底。batch 模式：SQLite（env.py render_as_batch）走表重建，
    #    PostgreSQL 原生 ALTER TABLE ADD CONSTRAINT（自动建同名唯一索引）。
    with op.batch_alter_table("media") as batch_op:
        batch_op.create_unique_constraint("uq_media_tmdb_id", ["tmdb_id"])


def downgrade() -> None:
    with op.batch_alter_table("media") as batch_op:
        batch_op.drop_constraint("uq_media_tmdb_id", type_="unique")