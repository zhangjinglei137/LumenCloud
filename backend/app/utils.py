"""tasks 层公共纯函数（公共化：集中各任务模块重复实现的工具，仅依赖标准库）。

职责：
- now_utc_naive()      统一 naive UTC 时间源（原各模块局部 _now 的重复实现）
- split_quark_path()   夸克完整路径 → (dir, [name])（transfer / recovery 共用）
- fmt_episode()        规范化集 key（scan / library_check 共用）
- parse_episode_num()  从集 key / 文件名提取集号（scan / library_check 共用）
- BoundedLRUCache()    进程内有界 LRU 缓存（tmdb / emby 进程内缓存有界化共用）

导入约定：tasks 层各模块从此处导入，不再各自重复定义；本模块只依赖标准库
（不依赖 app.* / sqlalchemy），避免任何循环导入与测试装配成本。
"""
import re
from collections import OrderedDict
from datetime import datetime, timezone

# 三重匹配（SxxExx / SxxExxx / 第N集）正则——常量仍留在各调用模块，
# 本文件仅保留函数实现所需的最小正则（_ep_num 用）
_RE_EP_NUM_SUFFIX = re.compile(r"E(\d{2,3})$")


class BoundedLRUCache:
    """进程内有界 LRU 缓存（审查 A1/A7：原模块级 dict 无淘汰，长运行后无界增长）。

    语义与 dict 兼容（get 未命中返回 None、支持 clear()/len()），额外提供：
    - get/set 命中或写入时 move_to_end（LRU 访问序）；
    - 写入后超上限（max_items）→ popitem(last=False) 淘汰最旧。

    说明：本类只管理「有界 + 淘汰」，不管理过期——TTL 判断仍由调用方完成
    （缓存值由调用方自行携带时间戳，如 tmdb/emby 的 `(timestamp, payload)`
    元组），命中且未过期的判定在调用方，与既有缓存语义保持一致。
    """

    def __init__(self, max_items: int) -> None:
        self._max_items = max_items
        self._d: OrderedDict = OrderedDict()

    def get(self, key):
        """读取：命中则刷新为最新访问（move_to_end）；未命中返回 None（dict 兼容）。"""
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def set(self, key, value) -> None:
        """写入：已存在先刷新位置；写入后超上限淘汰最旧条目。"""
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = value
        while len(self._d) > self._max_items:
            self._d.popitem(last=False)

    def __getitem__(self, key):
        """dict 风格读取（委托 get）：与既有 `_CACHE.get(key)` / `_CACHE[key]` 混用兼容。"""
        return self.get(key)

    def __setitem__(self, key, value) -> None:
        """dict 风格写入（委托 set）：既有 `_CACHE[key] = value` 调用点无需改写。"""
        self.set(key, value)

    def clear(self) -> None:
        """清空（既有测试依赖 _SEASON_AIR_CACHE.clear() / _INGESTED_CACHE.clear() 等）。"""
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)


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
    r"""从集 key / 文件名尾部提取集号（`E(\d{2,3})$` 结尾），取不到返回 None。

    原 scan.py / library_check.py 的 _ep_num（两者实现完全一致）。
    """
    m = _RE_EP_NUM_SUFFIX.search(filename_or_ep or "")
    return int(m.group(1)) if m else None
