"""tmdb_cache.number_of_episodes: TMDB TV 总集数列

Revision ID: 0014_tmdb_cache_episode_count
Revises: 0013_wait_since_admission_lock
Create Date: 2026-09-08

全量模式集号范围校验（少帅式搜索误匹配修复 A3）：tv 未收录全量扫描时，
用 TMDB 总集数拦截同名短剧/无关资源——「少帅」搜索入队 284 个错误资源案例中，
「少帅将我宠上天(99集)」等文件名集号 99 超过「少帅」48 集即拒绝入队。
总集数由 get_by_tmdb_id 回源 /3/tv/{id} 时取 number_of_episodes 缓存进
tmdb_cache 表（复用 7 天 TTL），避免对同一剧集反复回源。新增
number_of_episodes 列（Integer nullable）：仅 tv 有；movie 或无该字段 → NULL。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id 必须 ≤32 字符：alembic_version.version_num 在 Postgres 为
VARCHAR(32)，超长会报 StringDataRightTruncation（CI Postgres 迁移测试实证）。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_tmdb_cache_episode_count"
down_revision: Optional[str] = "0013_wait_since_admission_lock"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "tmdb_cache",
        sa.Column("number_of_episodes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tmdb_cache", "number_of_episodes")
