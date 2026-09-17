"""fix-audit-issues：外键 ondelete 级联策略 + task_run.media_id 类型对齐

Revision ID: 0017_audit_fixes
Revises: 0016_tmdb_cache_aliases
Create Date: 2026-09-17

背景（Task A1，命中风险信号：数据/schema 迁移）：
审查发现四个核心表（episode_state / task_queue / download_queue /
download_task）的 media 外键与 download_queue.task_queue_id 均无 ondelete 策略
——SQLite（PRAGMA foreign_keys=ON）与 PostgreSQL 下直接删除 media 父行会触发
FOREIGN KEY constraint failed；引用类（watch_requests.requested_by/reviewed_by、
invite_codes.used_by）无 ondelete 时删除用户同样被拦截。本迁移为子表外键补
CASCADE / SET NULL，保证「仅删除父行即可由 DB 层完成子行清理/置空」。

同时：
- task_run.media_id 由 INTEGER 对齐 BigInteger（与其余表 media_id 类型一致；
  PG 的 INTEGER 为 4 字节，装不下大整数 id 的场景）。
- episode_state.media_id 补独立索引 idx_eps_media（级联删除性能；task_queue
  已有 idx_tqk_media，不重复建）。

双后端差异处理：
- PostgreSQL：子表 FK 均为**未命名**约束（0001/0012 建表时未命名，PG 自动命名
  {表}_{列}_fkey）。升级按列动态查询 pg_constraint 实际约束名 drop 后重建命名 FK
  （兼容「未命名自动名」与「downgrade 重建的命名名」，保证 downgrade→upgrade
  往返一致性）；列类型用原生 ALTER。
- SQLite：FK 迁移只能靠表重建。关键坑：SQLite 在 PRAGMA foreign_keys=OFF 下
  CREATE TABLE 时**不会解析 FK 的 ON DELETE 动作**（一律 NO ACTION），故重建前
  必须先 op.execute(PRAGMA foreign_keys=ON)。重建采用「反射现有列/约束 →
  CreateTable 生成 DDL → 显式命名 FK（带 ondelete）→ 建临时表 → 拷贝数据 →
  drop 旧表 → rename」，彻底规避 alembic batch 保留未命名旧 FK 的问题；
  重建后恢复原索引。
  download_task.transfer_id、invite_codes.created_by 不在本任务级联范围，
  重建时显式保留无 ondelete 语义（避免 FK 随重建丢失）。

downgrade() 成对可逆：FK 还原为无 ondelete 命名约束，删除 idx_eps_media，
task_run.media_id 还原 INTEGER。

⚠ revision id ≤32 字符：alembic_version.version_num 在 Postgres 为 VARCHAR(32)。
"""
from typing import Optional, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, ForeignKeyConstraint, MetaData, Table
from sqlalchemy.schema import CreateTable

revision: str = "0017_audit_fixes"
down_revision: Optional[str] = "0016_tmdb_cache_aliases"
branch_labels: Optional[Sequence[str]] = None
depends_on: Optional[Sequence[str]] = None

# 每张表要重建的 FK：(约束名, 引用表, 本地列, ondelete)
# ondelete=None 保持无级联语义（任务范围外但重建需保留的 FK）。
_FK_SPECS: dict[str, list[tuple[str, str, str, Optional[str]]]] = {
    # ---- 媒体删除 → 子行级联清理（CASCADE）----
    "episode_state": [
        ("fk_episode_state_media_id_media", "media", "media_id", "CASCADE"),
    ],
    "task_queue": [
        ("fk_task_queue_media_id_media", "media", "media_id", "CASCADE"),
    ],
    "download_queue": [
        ("fk_download_queue_media_id_media", "media", "media_id", "CASCADE"),
        ("fk_download_queue_task_queue_id_task_queue", "task_queue", "task_queue_id", "CASCADE"),
    ],
    "download_task": [
        ("fk_download_task_media_id_media", "media", "media_id", "CASCADE"),
        ("fk_download_task_transfer_id_transfer_queue", "transfer_queue", "transfer_id", None),
    ],
    # ---- 用户删除 → 请求/邀请引用置空（SET NULL）----
    "watch_requests": [
        ("fk_watch_requests_requested_by_users", "users", "requested_by", "SET NULL"),
        ("fk_watch_requests_reviewed_by_users", "users", "reviewed_by", "SET NULL"),
    ],
    "invite_codes": [
        ("fk_invite_codes_created_by_users", "users", "created_by", None),
        ("fk_invite_codes_used_by_users", "users", "used_by", "SET NULL"),
    ],
}

# 重建后需要恢复/新加的索引：(表, 索引名, 列)
_INDEX_SPECS: dict[str, list[tuple[str, str, list[str]]]] = {
    "episode_state": [("idx_eps_media", "episode_state", ["media_id"])],
    "task_queue": [
        ("idx_tqk_status", "task_queue", ["status"]),
        ("idx_tqk_media", "task_queue", ["media_id"]),
        ("idx_tqk_silent_until", "task_queue", ["silent_until"]),
    ],
    "download_queue": [
        ("idx_dqk_status", "download_queue", ["status"]),
        ("idx_dqk_media", "download_queue", ["media_id"]),
    ],
}

# 参照表最小占位（SQLite 重建时新 FK 编译需 resolve 引用列）
_REFERENT_TABLES: list[str] = sorted(
    {ref for fks in _FK_SPECS.values() for _, ref, _, _ in fks}
)  # ['media', 'task_queue', 'transfer_queue', 'users']


def _sqlite_rebuild_table(op, table: str, fk_specs, indexes) -> None:
    """SQLite 表重建（FK 迁移）：反射列/约束 → 临时表(显式新 FK) → 拷贝 → drop → rename。"""
    bind = op.get_bind()
    m = MetaData()
    t = Table(table, m, autoload_with=bind)

    t2 = t.to_metadata(MetaData())
    t2.name = "_alembic_tmp_%s" % table
    # 旧 FK（列级内联 + 表级约束）随重建会被渲染，先清掉再显式重建
    for col in t2.columns:
        col.foreign_keys.clear()
    for const in list(t2.constraints):
        if isinstance(const, ForeignKeyConstraint):
            t2.constraints.remove(const)
    # 参照表占位（供 FKConstraint 编译时 resolve 引用列；反射可能已加空占位表）
    for ref in _REFERENT_TABLES:
        if ref not in t2.metadata.tables:
            Table(ref, t2.metadata, Column("id", sa.BigInteger()))
        elif "id" not in t2.metadata.tables[ref].c:
            t2.metadata.tables[ref].append_column(Column("id", sa.BigInteger()))
    for name, ref_table, column, ondelete in fk_specs:
        t2.append_constraint(
            ForeignKeyConstraint(
                [column], ["%s.id" % ref_table], name=name, ondelete=ondelete
            )
        )

    # 建临时表（列序与旧表一致）→ 拷贝数据 → 换名
    op.execute(sa.text(str(CreateTable(t2).compile(dialect=bind.dialect))))
    cols = ", ".join(c.name for c in t.columns)
    op.execute(
        sa.text(
            "INSERT INTO %s (%s) SELECT %s FROM %s"
            % (t2.name, cols, cols, table)
        )
    )
    op.execute(sa.text("DROP TABLE %s" % table))
    op.execute(sa.text("ALTER TABLE %s RENAME TO %s" % (t2.name, table)))

    # 恢复原索引 + 新增 idx_eps_media
    for idx_name, tbl, idx_cols in indexes:
        op.execute(
            sa.text(
                "CREATE INDEX %s ON %s (%s)"
                % (idx_name, tbl, ", ".join(idx_cols))
            )
        )


def _pg_fk_names(bind, table: str, column: str) -> list[str]:
    """PostgreSQL：返回 public 模式下引用 table.column 的实际 FK 约束名。

    0001/0012 建表的 FK 均未命名 → PG 自动命名 {表}_{列}_fkey；downgrade 重建后
    为迁移命名名（fk_*）。动态查询兼容两者，保证 drop 总命中实际约束。

    ⚠ 谓词写法说明：pg_constraint.conkey 是 int2vector（系统目录专用类型），
    「conkey = ARRAY(...)」与 int2[] 不是规范可比运算，存在静默 0 行风险
    （审查 R1 修复）；改用 pg_attribute.attnum = ANY(con.conkey) 标准写法。
    不额外过滤 fk_ 前缀：downgrade→upgrade 往返时旧迁移命名约束同样需要
    drop 后重建（create 同名约束会因重名失败），此处一并查出。
    """
    rows = bind.execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class cls ON cls.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN pg_attribute att
              ON att.attrelid = con.conrelid
             AND att.attnum = ANY(con.conkey)
            WHERE ns.nspname = 'public'
              AND cls.relname = :t
              AND con.contype = 'f'
              AND att.attname = :c
            """
        ),
        {"t": table, "c": column},
    ).fetchall()
    return [r[0] for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # PostgreSQL：先按列删除旧 FK（未命名自动名 / 命名名均可命中），再重建命名 FK
        for table, fks in _FK_SPECS.items():
            for name, ref_table, column, ondelete in fks:
                for old_name in _pg_fk_names(bind, table, column):
                    op.drop_constraint(old_name, table, type_="foreignkey")
        for table, fks in _FK_SPECS.items():
            for name, ref_table, column, ondelete in fks:
                op.create_foreign_key(
                    name, table, ref_table, [column], ["id"], ondelete=ondelete
                )
        op.create_index("idx_eps_media", "episode_state", ["media_id"])
        # task_run.media_id 类型对齐 BigInteger（PG: ALTER TYPE）
        op.alter_column("task_run", "media_id", type_=sa.BigInteger())
    else:
        # SQLite：必须先开 foreign_keys，否则重建出来的 FK 会被解析为 NO ACTION
        op.execute(sa.text("PRAGMA foreign_keys=ON"))
        # 重建顺序依赖：download_queue 引用 task_queue，须 task_queue 先重建
        for table in ("episode_state", "task_queue", "download_queue",
                      "download_task", "watch_requests", "invite_codes"):
            _sqlite_rebuild_table(
                op, table, _FK_SPECS[table], _INDEX_SPECS.get(table, [])
            )
        # task_run.media_id 类型对齐 BigInteger（SQLite: batch 表重建，无 FK 复用 batch）
        with op.batch_alter_table("task_run") as batch_op:
            batch_op.alter_column("media_id", type_=sa.BigInteger())


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    def _fk_without_ondelete(op, table: str, fks) -> None:
        """把命名 FK 重建为无 ondelete（还原 0016 语义）。"""
        for name, ref_table, column, ondelete in fks:
            op.create_foreign_key(
                name, table, ref_table, [column], ["id"], ondelete=None
            )

    if is_pg:
        for table, fks in _FK_SPECS.items():
            for name, ref_table, column, ondelete in fks:
                op.drop_constraint(name, table, type_="foreignkey")
        for table, fks in _FK_SPECS.items():
            _fk_without_ondelete(op, table, fks)
        op.drop_index("idx_eps_media", table_name="episode_state")
        op.alter_column("task_run", "media_id", type_=sa.Integer())
    else:
        op.execute(sa.text("PRAGMA foreign_keys=ON"))
        for table in ("episode_state", "task_queue", "download_queue",
                      "download_task", "watch_requests", "invite_codes"):
            # 重建为无 ondelete 的命名 FK（语义还原 0016）；索引保持
            no_op_fks = [(n, r, c, None) for (n, r, c, _) in _FK_SPECS[table]]
            # episode_state 的 idx_eps_media 是 0017 新增，降级不恢复（下面显式删）
            indexes = [] if table == "episode_state" else _INDEX_SPECS.get(table, [])
            _sqlite_rebuild_table(op, table, no_op_fks, indexes)
        # 0017 新增的 idx_eps_media 删除（降级还原）；task_queue/download_queue 原索引保留
        op.execute(sa.text("DROP INDEX IF EXISTS idx_eps_media"))
        with op.batch_alter_table("task_run") as batch_op:
            batch_op.alter_column("media_id", type_=sa.Integer())