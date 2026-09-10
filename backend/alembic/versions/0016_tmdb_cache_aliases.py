"""tmdb_cache.aliases: TMDB 别名持久化列

Revision ID: 0016_tmdb_cache_aliases
Revises: 0016_queue_size_estimated
Create Date: 2026-09-11

tmdb-alias-search-match（搜索匹配优化）：tmdb_cache 新增 aliases 可空列，
存 JSON 数组字符串（如 ["soul land","dou luo da lu"]，original_title +
also_known_as 归一化小写）。由 get_by_tmdb_id 详情回源时落库；search_multi
不写（search 响应无该字段）。后续搜索匹配读取该列做别名匹配，避免反复回源。

双后端兼容：SQLite 与 Postgres 均原生支持 ADD COLUMN（nullable=True 无默认值，
无需表重建 / server_default）；env.py 的 render_as_batch 对 SQLite 亦兼容。

⚠ revision id ≤32 字符：alembic_version.version_num 在 Postgres 为 VARCHAR(32)。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_tmdb_cache_aliases"
down_revision: Optional[str] = "0016_queue_size_estimated"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "tmdb_cache",
        sa.Column("aliases", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tmdb_cache", "aliases")
