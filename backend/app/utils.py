"""tasks 层公共纯函数（公共化：集中各任务模块重复实现的工具，仅依赖标准库）。

职责：
- now_utc_naive()      统一 naive UTC 时间源（原各模块局部 _now 的重复实现）
- split_quark_path()   夸克完整路径 → (dir, [name])（transfer / recovery 共用）
- fmt_episode()        规范化集 key（scan / library_check 共用）
- parse_episode_num()  从集 key / 文件名提取集号（scan / library_check 共用）

导入约定：tasks 层各模块从此处导入，不再各自重复定义；本模块只依赖标准库
（不依赖 app.* / sqlalchemy），避免任何循环导入与测试装配成本。
"""
import re
from datetime import datetime, timezone

# 三重匹配（SxxExx / SxxExxx / 第N集）正则——常量仍留在各调用模块，
# 本文件仅保留函数实现所需的最小正则（_ep_num 用）
_RE_EP_NUM_SUFFIX = re.compile(r"E(\d{2,3})$")


def now_utc_naive() -> datetime:
    """统一时间源：当前 UTC 时刻的 naive datetime（tzinfo=None）。

    即 `datetime.now(timezone.utc).replace(tzinfo=None)`——各任务模块原局部
    `_now` 的同一实现（与 models lane 的 server_default=func.now() 无时区 UTC
    字符串保持一致，避免 aware/naive 两种表示在 SQLite 中字符串比较不一致）。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def split_quark_path(path: str | None) -> tuple[str, list[str]]:
    """把夸克完整路径拆为 (dir, [name])，适配 alist.remove(names, dir) 契约。

    例如 /quark/movie.mkv → ("/quark/", ["movie.mkv"])；dir 以 / 结尾。
    （原 transfer.py / recovery.py 各自 _split_quark_path 的同一实现，保留原注释）
    """
    path = (path or "").strip()
    if not path:
        return "/", []
    path = path.rstrip("/")
    if "/" in path:
        dir_part, name = path.rsplit("/", 1)
        return (dir_part or "/") + "/", [name]
    return "/", [path]


def fmt_episode(season: int, ep: int) -> str:
    """规范化集 key：S01E01（两位）；三位集数（如 S01E100）保留三位。

    原 scan.py / library_check.py 的 _fmt_episode（两者实现完全一致）。
    """
    ep_s = f"E{ep:03d}" if ep >= 100 else f"E{ep:02d}"
    return f"S{season:02d}{ep_s}"


def parse_episode_num(filename_or_ep: str) -> int | None:
    """从集 key / 文件名尾部提取集号（`E(\d{2,3})$` 结尾），取不到返回 None。

    原 scan.py / library_check.py 的 _ep_num（两者实现完全一致）。
    """
    m = _RE_EP_NUM_SUFFIX.search(filename_or_ep or "")
    return int(m.group(1)) if m else None
