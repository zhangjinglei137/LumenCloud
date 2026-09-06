"""
LumenCloud 数据库备份脚本（阶段 4 生产化，交付 E）

功能：
  - SQLite（默认，DATABASE_URL 为空）：用 sqlite3 连接级 `.backup` API 做
    「在线安全备份」（对打开中的库也安全，WAL 模式下仍一致），复制
    <LUMENCLOUD_DATA_DIR>/lumencloud.db → scripts/backup/lumencloud_db_<ts>.db
  - Postgres（DATABASE_URL 含 postgres）：打印 pg_dump 备份指引，不自动执行
    （容器内 python:3.12-slim 大概率缺 pg_dump 客户端，避免误导）
  - 保留策略：只保留最近 N 份备份（默认 14，--keep 可调），旧备份自动清理；
    只按本脚本命名的 lumencloud_db_*.db 文件清理，不动其他备份（迁移备份等）

用法：
  python scripts/backup_db.py                    # 默认读当前环境变量，保留 14 份
  python scripts/backup_db.py --keep 30          # 保留最近 30 份
  python scripts/backup_db.py --env-file /path/.env.prod   # 显式加载 env 文件
  docker exec lumencloud python scripts/backup_db.py        # 容器内执行

cron 建议（宿主 crontab，容器内备份到挂载卷 scripts/backup/）：
  0 3 * * * cd /app && python scripts/backup_db.py
  或（容器外调度）：
  0 3 * * * docker exec lumencloud python scripts/backup_db.py

依赖：仅 Python 标准库（sqlite3/argparse/os/sys/pathlib），backend/.venv 或
系统 python3 均可直接运行；不导入 app.* 模块（避免触发服务端配置初始化）。
备份文件路径统一 scripts/backup/（与迁移回滚备份 scripts/backup/media_export_*.json 同目录）。
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BACKUP_DIR = Path(__file__).resolve().parent / "backup"
SQLITE_FILENAME = "lumencloud.db"
BACKUP_PREFIX = "lumencloud_db_"


def load_env_file(path: Path) -> None:
    """将 .env 文件键注入 os.environ（setdefault：不覆盖进程已存在的变量）。"""
    if not path.is_file():
        raise SystemExit(f"[错误] 指定的环境文件不存在: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"[错误] 无法读取环境文件 {path}: {exc}")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def mask_dsn(dsn: str) -> str:
    """DATABASE_URL 脱敏（日志不泄露密码）。"""
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
    return urlunsplit((parts.scheme, f"{user}:****@{host}{port}", parts.path, parts.query, parts.fragment))


def backup_sqlite(data_dir: Path) -> Path:
    """SQLite 在线安全备份（.backup API），返回备份文件路径。"""
    src = data_dir / SQLITE_FILENAME
    if not src.is_file():
        raise SystemExit(
            f"[错误] 未找到 SQLite 数据库文件: {src}\n"
            "       请确认应用已初始化（至少启动过一次）且 LUMENCLOUD_DATA_DIR 正确。"
        )

    backup_dir = BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 秒级时间戳 + 唯一性保护：同秒内重复备份不覆盖已有文件（追加 _2/_3 后缀）
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"{BACKUP_PREFIX}{base}.db"
    n = 2
    while dst.exists():
        dst = backup_dir / f"{BACKUP_PREFIX}{base}_{n}.db"
        n += 1

    # 源库以只读模式打开（避免干扰运行中的应用连接），.backup 到新连接
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    try:
        with sqlite3.connect(dst) as dst_conn:
            src_conn.backup(dst_conn)
    finally:
        src_conn.close()
    return dst


def prune_backups(keep: int) -> int:
    """只清理本脚本命名的 lumencloud_db_*.db，保留最近 keep 份。返回删除数。"""
    if keep < 1:
        return 0
    backups = sorted(BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.db"))
    removed = 0
    for old in backups[:-keep]:
        old.unlink()
        removed += 1
        print(f"[清理] 删除过期备份: {old.name}")
    return removed


def print_postgres_guide(database_url: str) -> None:
    """Postgres 场景：打印 pg_dump 指引（不自动执行）。"""
    print("[提示] 检测到 Postgres 数据库（DATABASE_URL 含 postgres）。")
    print("       本脚本不自动执行 Postgres 备份：容器内 python:3.12-slim 通常缺 pg_dump 客户端。")
    print("       请在能访问数据库的主机/容器内用 pg_dump 备份，参考命令：")
    print(f"          DATABASE_URL={mask_dsn(database_url)}")
    print("       pg_dump \"postgresql://<user>:<pass>@<host>:<port>/<db>\" \\")
    print("         -Fc -f scripts/backup/lumencloud_pg_$(date +%Y%m%d_%H%M%S).dump")
    print("       备份保留策略与清理建议用宿主 cron（如 find ... -mtime +14 -delete）。")
    print("       SQLite 在线备份逻辑不适用于 Postgres，两者互不影响。")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LumenCloud 数据库备份：SQLite 在线备份 / Postgres pg_dump 指引"
    )
    ap.add_argument("--keep", type=int, default=14, metavar="N",
                    help="保留最近 N 份备份（默认 14）")
    ap.add_argument("--env-file", default="", metavar="PATH",
                    help="可选：.env 文件路径（默认读取进程环境变量；容器内由 compose 注入）")
    args = ap.parse_args()

    if args.env_file:
        load_env_file(Path(args.env_file))
    if args.keep < 1:
        raise SystemExit("[错误] --keep 至少为 1（0 会连当前备份一起清掉）")

    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    data_dir = Path(os.environ.get("LUMENCLOUD_DATA_DIR", "data")).resolve()

    if database_url.startswith("postgres"):
        print_postgres_guide(database_url)
        # 保留策略仍对本脚本 SQLite 备份生效（无则 no-op），兼容双库混用场景
        prune_backups(args.keep)
        sys.exit(0)

    if database_url:
        print(f"[提示] DATABASE_URL 为非空但非 postgres 前缀（{database_url.split(':')[0]}），按 SQLite 备份处理。")

    dst = backup_sqlite(data_dir)
    removed = prune_backups(args.keep)
    print(f"[完成] SQLite 备份成功: {dst}")
    print(f"       保留最近 {args.keep} 份（本次清理 {removed} 份过期备份）。")
    print(f"       备份目录: {BACKUP_DIR}")


if __name__ == "__main__":
    main()
