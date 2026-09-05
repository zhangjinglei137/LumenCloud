"""scan 阶段 3 递归遍历改造单测（交付 F，docs/阶段1验证报告.md 结论 2/3 落地）。

覆盖：
- _walk_share：share-list 递归目录树（根 → 文件夹 → 具体文件），断言收集到的
  file_id / fid_token / size / path 正确；size 缺失（0/None）→ size_unknown 标记
- _walk_share：单条目分享 size 缺失时用 share-info fileSize 兜底（结论 3 case a）
- _walk_share：子目录 share-list 失败 → 跳过该分支，其余文件仍收集
- _enqueue_payload：G1 修复——info 自带顶层 fids（文件夹 fid）时仍输出「具体文件」fids
"""
import asyncio

from app.services import cloudsaver
from app.tasks import scan


# 两层目录树：根含 1 文件夹 + 1 文件；文件夹内含 2 个文件（其中 1 个 size 缺失）
def _build_tree():
    root = {
        "list": [
            {
                "fileName": "流浪地球2(2023)4K",
                "fileId": "folder_1",
                "fileIdToken": "ft_folder1",
                "isFolder": True,
                "size": 0,
            },
            {
                "fileName": "海报.jpg",
                "fileId": "pic_file",
                "fileIdToken": "ft_pic",
                "isFolder": False,
                "size": 2048,
            },
        ]
    }
    sub = {
        "list": [
            {
                "fileName": "S01E01.mkv",
                "fileId": "file_1",
                "fileIdToken": "ft_file1",
                "isFolder": False,
                "size": 1024,
            },
            {
                "fileName": "S01E02.mkv",
                "fileId": "file_2",
                "fileIdToken": "ft_file2",
                "isFolder": False,
                "size": None,  # 递归内层 size 字段缺失（阶段 1 结论 3）
            },
        ]
    }
    return root, sub


def test_walk_share_recurses_into_folder(monkeypatch):
    """根目录含文件夹 → 递归进文件夹收集具体文件；size 缺失分支标记 size_unknown。"""
    root, sub = _build_tree()

    async def fake_share_list(share_code, *, pdir_fid="", pwd_id="", stoken="", receive_code=""):
        assert pdir_fid in ("", "folder_1")
        return {"list": sub["list"] if pdir_fid else root["list"]}

    monkeypatch.setattr(cloudsaver, "share_list", fake_share_list)
    files = asyncio.run(scan._walk_share("abc123", {"fileSize": 9999}))

    assert len(files) == 3  # 文件夹内 2 个 + 根目录海报 1 个
    by_id = {f["file_id"]: f for f in files}

    f1 = by_id["file_1"]
    assert f1["file_name"] == "S01E01.mkv"
    assert f1["file_size"] == 1024
    assert f1["fid_token"] == "ft_file1"
    assert f1["path"] == "流浪地球2(2023)4K/S01E01.mkv"  # 相对路径含父目录
    assert f1["size_unknown"] is False

    # size 缺失且顶层 2 个条目（不满足单条目兜底）→ size=0 + size_unknown=True
    f2 = by_id["file_2"]
    assert f2["file_size"] == 0
    assert f2["size_unknown"] is True

    pic = by_id["pic_file"]
    assert pic["path"] == "海报.jpg"  # 根目录文件 path 即文件名
    assert pic["file_size"] == 2048
    assert pic["size_unknown"] is False


def test_walk_share_single_entry_uses_share_filesize(monkeypatch):
    """单条目分享（顶层仅 1 个文件且 size 缺失）→ 用 share-info fileSize 兜底。"""
    async def fake_share_list(share_code, *, pdir_fid="", pwd_id="", stoken="", receive_code=""):
        assert pdir_fid == ""
        return {"list": [
            {"fileName": "电影.mkv", "fileId": "f_mov", "fileIdToken": "ft_mov",
             "isFolder": False, "size": None},
        ]}

    monkeypatch.setattr(cloudsaver, "share_list", fake_share_list)
    files = asyncio.run(scan._walk_share("abc123", {"fileSize": 3758096384}))  # 3.5G

    assert len(files) == 1
    assert files[0]["file_size"] == 3758096384
    assert files[0]["size_unknown"] is False


def test_walk_share_skips_failed_subdir(monkeypatch):
    """子目录 share-list 失败 → try/except 跳过该分支，根目录文件仍被收集。"""
    root, _sub = _build_tree()
    calls = []

    async def fake_share_list(share_code, *, pdir_fid="", pwd_id="", stoken="", receive_code=""):
        calls.append(pdir_fid)
        if pdir_fid == "folder_1":
            raise RuntimeError("share-list 子目录失败")
        return {"list": root["list"]}

    monkeypatch.setattr(cloudsaver, "share_list", fake_share_list)
    files = asyncio.run(scan._walk_share("abc123", {}))

    assert "folder_1" in calls
    assert [f["file_id"] for f in files] == ["pic_file"]  # 海报仍在，文件夹分支安静跳过


def test_walk_share_respects_max_files(monkeypatch):
    """文件数达 max_files → 停止整棵遍历（防巨型分享拖垮）。"""
    async def fake_share_list(share_code, *, pdir_fid="", pwd_id="", stoken="", receive_code=""):
        return {"list": [
            {"fileName": f"f{i}.mkv", "fileId": f"file_{i}", "fileIdToken": f"ft{i}",
             "isFolder": False, "size": 100}
            for i in range(10)
        ]}

    monkeypatch.setattr(cloudsaver, "share_list", fake_share_list)
    files = asyncio.run(scan._walk_share("abc123", {}, max_files=3))

    assert len(files) == 3


def test_enqueue_payload_prefers_concrete_file_fids():
    """G1：info 自带顶层 fids（文件夹 fid，生产版不落盘）时仍输出具体文件 fids。"""
    info = {
        "pwd_id": "pd",
        "stoken": "st",
        "receive_code": "",
        "fids": ["folder_fid_xxx"],      # 顶层文件夹 fids（阶段 1 实证不落盘）
        "fid_tokens": ["ft_folder"],
        "folder_id": "fold1",
        "fileSize": 9999,
    }
    f = {"file_id": "file_1", "fid_token": "ft_file1", "file_size": 1024}
    payload = scan._enqueue_payload(info, f)

    assert payload["fids"] == ["file_1"]
    assert payload["fid_tokens"] == ["ft_file1"]
    assert "folder_id" not in payload  # 顶层 folder_id 一并忽略
    # 凭据继续取自 share-info
    assert payload["pwd_id"] == "pd"
    assert payload["stoken"] == "st"
    assert payload["receive_code"] == ""

# ---- 阶段 3 真实链路验证发现：全量模式视频扩展名过滤（对齐 n8n ALLOWED_EXTENSIONS）----

def test_is_video_file_filter():
    """视频扩展名白名单过滤：mp4/mkv 放行，jpg/srt/nfo/无扩展名拒绝（对齐 n8n）。"""
    from app.tasks.scan import _is_video_file
    assert _is_video_file("Top011.Inception.2010.mp4")
    assert _is_video_file("S01E01.mkv")
    assert _is_video_file("大写的.MKV")
    assert not _is_video_file("Cover.jpg")
    assert not _is_video_file("字幕.srt")
    assert not _is_video_file("movie.nfo")
    assert not _is_video_file("README")
    assert not _is_video_file("")
