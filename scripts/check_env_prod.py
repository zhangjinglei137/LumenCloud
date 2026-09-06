"""
LumenCloud 生产环境校验（阶段 4 生产化，交付 E + Phase 8 配置入库）

★ Phase 8 起 .env.prod 已【非必需】：
   - 服务凭据全部经设置页配置入库（system_config），保存即生效、无需重启；
   - JWT 签名密钥首启自动生成落盘 <data_dir>/.jwt_secret（chmod 600）；
   - admin 初始口令首启自动生成并打印在服务日志（docker logs），登录后设置页修改。
   - 因此 docker compose up 无需 .env.prod；本脚本仅作【可选的既有 .env.prod 复查】：
     若文件存在，校验其中的旧式 env 键是否弱值（防用户误用占位值上线）。

用法：
  python scripts/check_env_prod.py                # 默认读 ./.env.prod
  python scripts/check_env_prod.py --env-file /path/to/.env.prod

退出码：
  0    .env.prod 不存在（Phase 8 免 env，正常通过）或全部键值合格
  1    .env.prod 存在但含缺失/弱值键（此时不应直接上线，建议改用设置页配置）

安全：
  只读文件，无第三方依赖；输出仅列缺席/不合格的键名，绝不回显任何键值。
"""
import argparse
import os
import sys
from pathlib import Path

# (键名, 说明, 最低长度；None 表示仅非空) —— 仅当 .env.prod 存在时复查这些键
# Phase 8 起上述键全部「可选经设置页入库」；若用户仍提供 .env.prod，则按其校验
_REQUIRED = [
    ("JWT_SECRET", "JWT 签名密钥（≥16 字符且非 change_me；Phase 8 起可省略，自动落盘）", 16),
    ("INIT_ADMIN_PASSWORD", "初始管理员口令（≥8 字符且非 change_me；Phase 8 起可省略，随机生成）", 8),
    ("ALIST_BASE_URL", "AList 服务地址（可选，设置页配置）", None),
    ("ALIST_TOKEN", "AList 管理 TOKEN（可选，设置页配置）", None),
    ("ARIA2_RPC_URL", "Aria2 RPC 地址（可选，设置页配置）", None),
    ("ARIA2_TOKEN", "Aria2 RPC 密钥（可选，设置页配置）", None),
    ("CLOUDSAVER_BASE_URL", "CloudSaver 服务地址（可选，设置页配置）", None),
    ("CLOUDSAVER_USERNAME", "CloudSaver 账号（可选，设置页配置）", None),
    ("CLOUDSAVER_PASSWORD", "CloudSaver 口令（可选，设置页配置）", None),
    ("QUARK_DEFAULT_FOLDER", "夸克中转目录 folderId（可选，设置页配置）", None),
    ("TMDB_API_KEY", "TMDB API Key（可选，设置页配置）", None),
]

_WEAK_PLACEHOLDERS = {"change_me", "changeme", "your_password", "changeme123"}
# M1（Oracle Gate3）：本脚本是 pre-flight 加严——比后端启动护栏（auth.py
# _assert_secure_secrets，仅拒绝精确 "change_me"）更严格地拒绝常见占位值，
# 二者不矛盾：脚本拦住的弱值即便侥幸过了启动护栏也会在部署前被纠正。


def parse_env_file(path: Path) -> dict:
    """极简 .env 解析：忽略注释/空行，支持 `export ` 前缀与首尾引号。

    不做行内注释剥离（本项目 .env.example 无行内注释，避免误截含 # 的值）。
    后出现的同名键覆盖前值（dotenv 惯例）。失败键不计入。
    """
    env: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"[错误] 无法读取环境文件 {path}: {exc}")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def is_weak(value: str, min_len: int | None) -> str | None:
    """返回弱值原因（无问题返回 None）。只判断值强度，不输出值本身。"""
    if not value:
        return "空值"
    if min_len and len(value) < min_len:
        return f"过短（需 ≥{min_len} 字符）"
    if value.strip().lower() in _WEAK_PLACEHOLDERS:
        return "使用默认占位值（change_me 等）"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="LumenCloud .env.prod 必填键强校验（fail-fast，对齐启动护栏）"
    )
    ap.add_argument(
        "--env-file",
        default=".env.prod",
        metavar="PATH",
        help="环境文件路径（默认 ./.env.prod）",
    )
    args = ap.parse_args()

    env_file = Path(args.env_file).resolve()
    if not env_file.is_file():
        print(f"[失败] 环境文件不存在: {env_file}")
        print("       请从 .env.example 复制并填写后重试（.env.prod 已被 gitignore，不进仓库）。")
        sys.exit(1)

    env = parse_env_file(env_file)

    problems: list[str] = []
    for key, desc, min_len in _REQUIRED:
        reason = is_weak(env.get(key, ""), min_len)
        if reason:
            problems.append(f"  {key:<24}（{desc}）：{reason}")

    if problems:
        print("[失败] .env.prod 校验未通过，以下必填项缺失/不合格（出于安全不回显键值）：")
        print("\n".join(problems))
        print("\n请补齐后重新运行本脚本；全部通过后再执行 docker compose up。")
        sys.exit(1)

    print(f"[通过] {env_file} 必填项全部就绪（{len(_REQUIRED)} 项，不含可选项）。")
    sys.exit(0)


if __name__ == "__main__":
    main()
