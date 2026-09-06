"""alist.list_dir 分页循环单测（C-2）。

不连真实 alist：monkeypatch app.services.alist._post，按 body["page"] 分发各页
content 条数（stub 模式，同 test_capacity 的 mock 思路）。
"""
import os
import tempfile

# 测试环境隔离（收敛修复）：本文件按 pytest 字母序最先被收集，若不在导入任何
# app 模块前清空外部服务凭据，app.config.settings 会被项目根 .env 的真实凭据
# 实例化并缓存，导致其后 test_api_smoke 顶部 os.environ 覆盖失效、批准流程触发
# 真实 cloudSaver 联网入队。此处仿 test_delete_media_fk.py 模式前置隔离。
_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_alist_pg_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
for _K in (
    "TMDB_API_KEY", "TMDB_PROXY", "CLOUDSAVER_BASE_URL", "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD", "EMBY_BASE_URL", "EMBY_API_KEY", "ALIST_BASE_URL",
    "ALIST_TOKEN", "ARIA2_RPC_URL", "ARIA2_TOKEN", "NASTOOLS_BASE_URL", "PUSHPLUS_TOKEN",
):
    os.environ[_K] = ""

import asyncio

from app.services.alist import list_dir


def run(coro):
    return asyncio.run(coro)


class _FakePost:
    """_post 的 stub：pages 为每页 content 条数（index 0 → page 1，未知页返回空）。

    记录每次请求体（calls）与 page 序列（seen_pages），供断言分页行为。
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.seen_pages = []

    async def __call__(self, path, body):
        self.calls.append(dict(body))
        page = body["page"]
        self.seen_pages.append(page)
        # 每页均按真实请求语义校验固定字段（防回归）
        assert path == "/api/fs/list"
        assert body["password"] == ""
        assert body["refresh"] is True
        assert body["per_page"] == 1000
        n = self.pages[page - 1] if 1 <= page <= len(self.pages) else 0
        return {"content": [
            {"name": f"item-{page}-{i}", "is_dir": False, "size": 0}
            for i in range(n)
        ]}


# ---- 分页循环：满页（==per_page）继续，短页（<per_page，含 0 条）停止 ----

def test_list_dir_paginates_until_short_page(monkeypatch):
    """三页 1000/1000/3：返回 2003 条、_post 调用 3 次、page 依次 1/2/3。"""
    fake = _FakePost([1000, 1000, 3])  # 前两页满 1000 → 继续；第三页 3 条 → 尾页
    monkeypatch.setattr("app.services.alist._post", fake)

    entries = run(list_dir("/quark"))

    assert len(entries) == 2003
    assert len(fake.calls) == 3
    assert fake.seen_pages == [1, 2, 3]
    assert entries[0]["name"] == "item-1-0"
    assert entries[2000]["name"] == "item-3-0"  # 跨页顺序合并


def test_list_dir_single_short_page(monkeypatch):
    """单页 3 条（不足 1000）：只调 1 次、返回 3 条。"""
    fake = _FakePost([3])
    monkeypatch.setattr("app.services.alist._post", fake)

    entries = run(list_dir("/quark"))

    assert len(entries) == 3
    assert len(fake.calls) == 1
    assert fake.seen_pages == [1]


def test_list_dir_empty_page(monkeypatch):
    """空目录（0 条）：调 1 次、返回 []（0 < per_page → 尾页）。"""
    fake = _FakePost([0])
    monkeypatch.setattr("app.services.alist._post", fake)

    entries = run(list_dir("/quark"))

    assert entries == []
    assert len(fake.calls) == 1
    assert fake.seen_pages == [1]


# ---- 条目归一化：name / is_dir(bool) / size（缺省 0）保持既有形态 ----

def test_list_dir_entry_normalization(monkeypatch):
    """归一化字段形态：is_dir 做 bool 转换、size 缺省为 0。"""
    content = [
        {"name": "sub", "is_dir": 1, "size": None},   # is_dir=1 → True；size None → 0
        {"name": "a.mkv", "is_dir": 0, "size": 42},   # is_dir=0 → False
        {"name": "b.mkv", "is_dir": "true", "size": 0},
    ]

    async def fake(path, body):
        return {"content": content}

    monkeypatch.setattr("app.services.alist._post", fake)

    entries = run(list_dir("/quark"))

    assert entries == [
        {"name": "sub", "is_dir": True, "size": 0},
        {"name": "a.mkv", "is_dir": False, "size": 42},
        {"name": "b.mkv", "is_dir": True, "size": 0},  # bool("true") → True
    ]
    assert all(type(e["is_dir"]) is bool for e in entries)