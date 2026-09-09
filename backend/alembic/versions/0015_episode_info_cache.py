"""episode_info_cache: TMDB 集信息持久化缓存表（episode-status-cache 需求）

- tmdb_id + season + episode 唯一，按影视刷新、按集查询
- name/air_date 来自 TMDB season episodes（air_date 为 "YYYY-MM-DD" 或 NULL）
- 双后端（SQLite/PG）兼容：id 用 BigInteger+Identity（SQLite 退化为 INTEGER）
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_episode_info_cache"
down_revision = "0014_tmdb_cache_episode_count"

BIG_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "episode_info_cache",
        sa.Column("id", BIG_PK, sa.Identity(), primary_key=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("tmdb_id", "season", "episode", name="uq_episode_info_cache_tmdb_season_episode"),
    )


def downgrade() -> None:
    op.drop_table("episode_info_cache")