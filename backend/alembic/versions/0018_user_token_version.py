"""users.token_version: 修改密码吊销既有令牌

Revision ID: 0018_user_token_version
Revises: 0017_audit_fixes
Create Date: 2026-09-17

背景（Task B1，命中风险信号：认证/授权）：
审查 C3 —— 7 天有效期窗口内旧 token 在改密后持续有效。users 表新增
token_version 列（默认 0），改密事务内递增；JWT payload 携带 ver，鉴权时
比对版本一致。存量 token（无 ver）视为版本 0——用户改密前仍有效，改密后
（版本 ≥1）即失效。

双后端兼容：SQLite 与 PostgreSQL 均原生支持带 server_default 的 ADD COLUMN
（现有行自动填 0，nullable=False 无需表重建）；与 0016 同为简单加列场景。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op

# ⚠ revision id ≤32 字符：alembic_version.version_num 在 Postgres 为 VARCHAR(32)。
revision: str = "0018_user_token_version"
down_revision: Optional[str] = "0017_audit_fixes"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")