"""
LumenCloud 阶段 3：旧 n8n Postgres media 表 → 新库迁移脚本
（docs/新系统设计.md §12.1，旧表实际列见 docs/实施计划.md §六）

功能：
  读取旧 n8n 库的 media 表（实际列 media/tmdb_id，varchar），做 tmdb_id 的
  varchar→integer 清洗后，写入 LumenCloud 新库（默认 SQLite，或 env DATABASE_URL）。

用法：
  # 只读预览（默认模式）：打印待写入/跳过/非法值/TMDB 补查状态，不写新库
  python scripts/migrate_from_n8n.py --dsn postgresql://user:pass@host:5432/db
  N8N_DB_DSN=postgresql://... python scripts/migrate_from_n8n.py        # DSN 走环境变量

  # 实际写入（--apply 前自动导出回滚备份到 scripts/backup/）
  python scripts/migrate_from_n8n.py --dsn postgresql://... --apply

  # 容器内执行（阶段 4 上线）
  docker exec lumencloud python scripts/migrate_from_n8n.py --dsn postgresql://... --apply

  # 回滚恢复（从备份 JSON 恢复到迁移前快照）
  python scripts/migrate_from_n8n.py --restore scripts/backup/media_export_<ts>.json

护栏：
  - 不带 --apply 只做 dry-run（只读预览，不写新库、不写备份）
  - psycopg2 惰性 import：脚本可正常编译/预览；旧库连接时才需要驱动
  - 旧表列名防御：information_schema 核对 media/tmdb_id 实际列，列名不符即退出，不臆测
  - tmdb_id CAST（P6）：空串/NULL→None；纯数字→int；含非数字→正则提取首个数字串
    （如 "tt123"→123，记警告）；提取不出→None 记警告；全程不中断
  - 幂等键：tmdb_id 非空以其为键；为空则以 title.strip().lower() 归一化键；已存在跳过
  - --apply 前自动导出备份 JSON（新库已有 media 行 + 本次待写入行）+ 打印回滚指引
  - 敏感信息不落日志（DSN 密码脱敏、不打印凭据）
"""
import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# 允许从任意 cwd 运行：backend 目录加入 sys.path（参考 scripts/probe_capacity.py）
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

from app.config import settings  # noqa: E402（.env 凭据：DATABASE_URL / TMDB_API_KEY）
from app.database import async_session  # noqa: E402
from app.models import Media  # noqa: E402

# ---- tmdb_id 清洗（P6：varchar→integer）----
# CAST 结果分类：
#   empty    空串/NULL → None（无非法）
#   digit    纯数字   → int（标准）
#   extracted 含非数字但正则提取到首个数字串 → int（原值不标准，记警告计数）
#   invalid  含非数字且提取不出任何数字 → None（记警告计数）
CAST_EMPTY = "empty"
CAST_DIGIT = "digit"
CAST_EXTRACTED = "extracted"
CAST_INVALID = "invalid"

_DIGITS_RE = re.compile(r"(\d+)")


def mask_dsn(dsn: str) -> str:
    """DSN 密码脱敏（日志/提示不泄露凭据）。非 URL 形式时直接模糊主机部分。"""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "postgresql://****@<host>/<db>"
    if not parts.scheme:
        return "postgresql://****@<host>/<db>"
    user = parts.username or ""
    host = parts.hostname or "<host>"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:****@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def cast_tmdb_id(raw) -> tuple:
    """旧表 tmdb_id varchar → (int|None, 分类)。返回的第二个字段用于非法值统计。"""
    if raw is None:
        return None, CAST_EMPTY
    s = str(raw).strip()
    if not s:
        return None, CAST_EMPTY
    if s.isdigit():
        try:
            return int(s), CAST_DIGIT
        except ValueError:  # 全角数字等 isdigit 误判，落到正则
            pass
    m = _DIGITS_RE.search(s)
    if m:
        return int(m.group(1)), CAST_EXTRACTED
    return None, CAST_INVALID


def normalize_title(title: str) -> str:
    """标题归一化幂等键（tmdb_id 为空时的兜底）。"""
    return (title or "").strip().lower()


# ---- 旧库读取（同步 psycopg2 驱动，惰性 import）----
def read_old_rows(dsn: str) -> list:
    """连接旧 Postgres 读取 media 表。

    防御：先用 information_schema 核对实际列名（旧表列名不符不臆测，直接退出）。
    返回: [(media, tmdb_id), ...]
    """
    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"[错误] 当前环境缺少 psycopg2 驱动（缺失模块: {exc.name}），无法连接旧库。\n"
            "      请确认已安装 backend/requirements.txt 中的 psycopg2-binary。"
        )

    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"[错误] 连接旧库失败（{mask_dsn(dsn)}）: {exc}")

    try:
        with conn.cursor() as cur:
            # information_schema 防御：查实际列，列名不符 → 报告并退出，不臆测
            cur.execute(
                "SELECT table_schema, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_name = 'media' "
                "ORDER BY table_schema, ordinal_position"
            )
            cols = cur.fetchall()  # [(schema, column, type), ...]
        actual_cols = {c[1] for c in cols}
        if not actual_cols:
            raise SystemExit("[错误] 旧库 information_schema 中未找到 media 表，拒绝迁移。")
        missing = [n for n in ("media", "tmdb_id") if n not in actual_cols]
        if missing:
            schemas = sorted({c[0] for c in cols})
            print(f"[旧库] media 表实际列: {sorted(actual_cols)}（schema: {schemas}）")
            raise SystemExit(
                f"[错误] 旧表 media 缺少预期列 {missing}，不做臆测，请核对后调整本脚本。"
            )

        with conn.cursor() as cur:
            cur.execute('SELECT media, tmdb_id FROM media')
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


# ---- 新库读取 / 写入 ----
def _row_to_dict(r) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "tmdb_id": r.tmdb_id,
        "media_type": r.media_type,
        "status": r.status,
        "scan_interval_minutes": r.scan_interval_minutes,
        "max_episode_size_gb": r.max_episode_size_gb,
        "max_movie_size_gb": r.max_movie_size_gb,
        "in_emby": bool(r.in_emby),
        "last_scan_at": r.last_scan_at.isoformat() if r.last_scan_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


async def load_existing_media() -> tuple:
    """读新库 media 表已有行。返回 (rows, tmdb_id 幂等集合, title 归一化幂等集合)。

    表不存在（新库未初始化建表）→ 提示并按空库处理；绝不停在 traceback。
    """
    async with async_session() as session:
        try:
            res = await session.execute(
                text("SELECT id, title, tmdb_id, media_type, status, scan_interval_minutes, "
                     "max_episode_size_gb, max_movie_size_gb, in_emby, last_scan_at, "
                     "created_at, updated_at FROM media ORDER BY id")
            )
            rows = list(res.fetchall())
        except DBAPIError as exc:
            print(f"[提示] 新库 media 表尚未建表（{type(exc).__name__}），按空库处理。")
            if settings.DATABASE_URL:
                print("        DATABASE_URL 已配置：请先执行 `alembic upgrade head` 建表。")
            return [], set(), set()

    tmdb_set: set = set()
    title_set: set = set()
    for r in rows:
        if r.tmdb_id is not None:
            tmdb_set.add(int(r.tmdb_id))
        title_set.add(normalize_title(r.title or ""))
    return rows, tmdb_set, title_set


def plan_rows(rows: list, existing_tmdb: set, existing_titles: set) -> tuple:
    """CAST + 幂等判定（纯同步，可单测）。

    返回 (plan, stats)：
      plan: 每行 {title, raw_tmdb, tmdb_id, cast_kind, action: write/skip, media_type}
      stats: {read, write, skip, cast_empty, cast_digit, cast_extracted, cast_invalid, media_type_pending}
    """
    stats = {
        "read": len(rows),
        "write": 0,
        "skip": 0,
        "cast_empty": 0,
        "cast_digit": 0,
        "cast_extracted": 0,
        "cast_invalid": 0,
        "media_type_pending": 0,
    }
    plan = []
    seen_tmdb = set(existing_tmdb)  # 拷贝，避免污染入参；同时覆盖旧表内部重复
    seen_titles = set(existing_titles)

    for raw_media, raw_tmdb in rows:
        title = str(raw_media).strip()
        tmdb_id, kind = cast_tmdb_id(raw_tmdb)
        stats[f"cast_{kind}"] += 1

        if tmdb_id is not None:
            dup = tmdb_id in seen_tmdb
            key = ("tmdb", tmdb_id)
        else:
            nkey = normalize_title(title)
            dup = nkey in seen_titles
            key = ("title", nkey)

        item = {
            "title": title,
            "raw_tmdb": str(raw_tmdb) if raw_tmdb is not None else None,
            "tmdb_id": tmdb_id,
            "cast_kind": kind,
            "action": "skip" if dup else "write",
            "media_type": None,
        }
        if dup:
            stats["skip"] += 1
        else:
            stats["write"] += 1
            if key[0] == "tmdb":
                seen_tmdb.add(key[1])
            else:
                seen_titles.add(key[1])
        plan.append(item)
    return plan, stats


async def enrich_media_type(plan: list) -> list:
    """对将写入的行做 TMDB search_multi 补查 media_type。

    失败 / 未配 key / 无 movie/tv 结果 → media_type 留 None，计入「待补」日志，不中断。
    """
    if not settings.TMDB_API_KEY:
        print("[TMDB] TMDB_API_KEY 未配置 → media_type 全部标记「待补」，迁移继续进行")
    pending = 0
    for item in plan:
        if item["action"] != "write":
            continue
        mt = await _fetch_media_type(item["title"])
        item["media_type"] = mt
        if mt is None:
            pending += 1
            print(f"[TMDB] ⚠ 待补 media_type: {item['title'][:40]}")
        else:
            print(f"[TMDB] {item['title'][:40]} → media_type={mt}")
    return plan


async def _fetch_media_type(title: str):
    """单条 TMDB 补查；任何失败返回 None（不抛）。"""
    from app.services import tmdb

    try:
        results = await tmdb.search_multi(title)
    except Exception as exc:  # noqa: BLE001
        print(f"[TMDB] 补查失败（{type(exc).__name__}）: {exc}")
        return None
    for r in results:
        mt = (r.get("media_type") or "").strip()
        if mt in ("movie", "tv"):
            return mt
    return None


async def export_backup(existing_rows: list, plan: list, mode: str) -> Path:
    """--apply 前导出回滚备份：新库已有行 + 本次待写入行。"""
    backup_dir = Path(__file__).resolve().parent / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / f"media_export_{datetime.now():%Y%m%d_%H%M%S}.json"
    payload = {
        "schema": "media_export",
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "note": "回滚备份：existing_rows=迁移前新库已有行；pending_rows=本次迁移待写入行。",
        "existing_rows": [_row_to_dict(r) for r in existing_rows],
        "pending_rows": [
            {"title": i["title"], "tmdb_id": i["tmdb_id"], "media_type": i["media_type"]}
            for i in plan if i["action"] == "write"
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[备份] 已导出回滚备份: {path}")
    return path


async def apply_write(plan: list) -> int:
    """写入待写行（单事务；幂等可重跑）。"""
    to_add = [
        Media(
            title=i["title"],
            tmdb_id=i["tmdb_id"],
            media_type=i["media_type"],
            status="tracking",
            in_emby=False,
        )
        for i in plan if i["action"] == "write"
    ]
    if to_add:
        async with async_session() as session:
            session.add_all(to_add)
            await session.commit()
    return len(to_add)


# ---- 回滚恢复（--restore，显式传入备份 JSON 才执行）----
def _parse_dt(v):
    if not v:
        return None
    return datetime.fromisoformat(str(v))


async def run_restore(json_path: str) -> None:
    """从备份 JSON 恢复到「迁移前快照」（清空 media 后写回 existing_rows）。"""
    p = Path(json_path)
    if not p.is_file():
        raise SystemExit(f"[错误] 备份文件不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("schema") != "media_export":
        raise SystemExit(f"[错误] 不是合法的 media_export 备份: {p}")
    existing = data.get("existing_rows", [])
    print("=" * 70)
    print(f"[restore] 备份: {p.name}")
    print(f"[restore] 将清空当前 media 表并写回 {len(existing)} 行（迁移前快照）。")
    print("[!] 此操作会删除新库 media 表当前全部数据，仅回滚时使用！")
    print("=" * 70)
    if input("输入 YES 确认执行: ").strip() != "YES":
        print("[restore] 已取消，未做任何修改。")
        return
    async with async_session() as session:
        await session.execute(text("DELETE FROM media"))
        for r in existing:
            session.add(
                Media(
                    id=r["id"],
                    title=r["title"],
                    tmdb_id=r["tmdb_id"],
                    media_type=r["media_type"],
                    status=r.get("status") or "tracking",
                    scan_interval_minutes=r.get("scan_interval_minutes") or 60,
                    max_episode_size_gb=r.get("max_episode_size_gb"),
                    max_movie_size_gb=r.get("max_movie_size_gb"),
                    in_emby=bool(r.get("in_emby", False)),
                    last_scan_at=_parse_dt(r.get("last_scan_at")),
                    updated_at=_parse_dt(r.get("updated_at")),
                )
            )
        await session.commit()
    print(f"[restore] 完成：已恢复 {len(existing)} 行。")


def print_report(stats: dict, mode: str) -> None:
    pen_w = "将写入" if mode == "dry-run" else "已写入"
    skip_s = "将跳过" if mode == "dry-run" else "已跳过"
    illegal = stats["cast_extracted"] + stats["cast_invalid"]
    print("=" * 70)
    print(f"[迁移报告]（{mode}）")
    print(f"  旧库读取       : {stats['read']} 条")
    print(f"  {pen_w}        : {stats['write']} 条")
    print(f"  {skip_s}（幂等命中）: {stats['skip']} 条")
    print(f"  tmdb_id 非法值 : {illegal} 条（已提取首数字串 {stats['cast_extracted']} / 无数字置 None {stats['cast_invalid']}）")
    if illegal:
        print("  提示：非法值均已按 P6 规则清洗，原始值请参照旧库核验。")
    print(f"  media_type 待补: {stats['media_type_pending']} 条")
    print("=" * 70)


def print_rollback_guide(backup_path: Path) -> None:
    print("[回滚指引] 仅当需要撤销本次迁移时使用：")
    print("  1) 备份文件已生成: " + str(backup_path))
    print("  2) 重置 media 表（危险，删除全部 media 行）:")
    print("       DELETE FROM media;    # 用 psql / sqlite3 客户端按新库类型执行")
    print("  3) 按备份恢复（推荐用内置恢复模式，恢复到迁移前快照）:")
    print("       python scripts/migrate_from_n8n.py --restore " + str(backup_path))
    print("     或人工依据备份 JSON 的 existing_rows 逐条重新 INSERT。")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="旧 n8n media 表 → LumenCloud 新库迁移（docs/新系统设计.md §12.1）"
    )
    ap.add_argument("--dsn", default="", help="旧 n8n Postgres DSN（默认取环境变量 N8N_DB_DSN）")
    ap.add_argument("--dry-run", action="store_true", help="只读预览（默认行为，不带 --apply 即 dry-run）")
    ap.add_argument("--apply", action="store_true", help="实际写入（自动导出回滚备份并打印回滚指引）")
    ap.add_argument("--restore", default="", metavar="JSON", help="从备份 JSON 恢复（回滚）")
    args = ap.parse_args()

    if args.restore:
        await run_restore(args.restore)
        return
    if args.apply and args.dry_run:
        ap.error("--apply 与 --dry-run 互斥")

    dsn = args.dsn or os.environ.get("N8N_DB_DSN", "").strip()
    if not dsn:
        raise SystemExit(
            "[错误] 缺少旧库 DSN：请用 --dsn 指定或设置环境变量 N8N_DB_DSN\n"
            "  示例: python scripts/migrate_from_n8n.py --dsn "
            "postgresql://user:pass@host:5432/db    （不带 --apply 只做只读预览）"
        )

    mode = "apply" if args.apply else "dry-run"
    print(f"==================== 迁移开始（{mode}）====================")
    print(f"旧库连接: {mask_dsn(dsn)}")
    print(f"新库: {settings.DATABASE_URL or '内置 SQLite（LUMENCLOUD_DATA_DIR/' + settings.LUMENCLOUD_DATA_DIR + '）'}")

    # 1. 读旧库（同步 psycopg2）
    old_rows = read_old_rows(dsn)
    print(f"[旧库] media 表读取 {len(old_rows)} 条（实际列 media/tmdb_id）")

    # 2. 读新库已有行做幂等键
    existing_rows, existing_tmdb, existing_titles = await load_existing_media()
    print(f"[新库] 已有 {len(existing_rows)} 行（幂等键: tmdb_id + title 归一化）")

    # 3. CAST + 幂等判定 + TMDB 补查
    plan, stats = plan_rows(old_rows, existing_tmdb, existing_titles)
    plan = await enrich_media_type(plan)
    stats["media_type_pending"] = sum(
        1 for i in plan if i["action"] == "write" and i["media_type"] is None
    )

    backup_path: Path | None = None
    if mode == "apply":
        # 4. 写前自动导出回滚备份（新库已有行 + 待写入行）
        backup_path = await export_backup(existing_rows, plan, mode)
        # 5. 写入
        written = await apply_write(plan)
        status = "success" if written == stats["write"] else "partial"
        print(f"[写入] 本次成功写入 {written} 条")
        if status == "partial":
            print(f"[警告] 期望写入 {stats['write']} 条但实际 {written} 条，请检查后重跑（幂等可重跑）")
    else:
        if stats["write"] == 0:
            print("[dry-run] 无待写入行（全部幂等命中/空表），无需 --apply。")
        else:
            print(f"[dry-run] 将写入 {stats['write']} 条（未做任何修改，也未导出备份）")

    print_report(stats, mode)
    if backup_path is not None:
        print_rollback_guide(backup_path)


if __name__ == "__main__":
    asyncio.run(main())