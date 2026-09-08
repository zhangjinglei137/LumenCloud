"""`_walk_share` 大小均摊估算回归测试（凡人修仙传 S01E190 一直无法入队的根因）。

背景证据链（生产实测）：
- cloudSaver `/api/quark/share-list` 返回条目仅有 {fileId, fileIdToken, fileName, isFolder}，
  **任何层级都没有 size 字段**；
- 旧逻辑把 share-info 总大小（如 4K 分享 361.38GB）赋给「顶层单文件夹分享」的每个文件
  → 190.mkv 被虚报 361GB → 超过 max_episode_size_gb=5 被过滤 → 永不入队。
- 修复：移除该兜底，改为 walk 完成后按「分享总大小 / 视频文件数」均摊估算
  （361GB/190≈1.9GB/集，通过大小过滤并可正常参与容量记账）。
"""
import asyncio
from unittest.mock import patch

from app.tasks.scan import _walk_share


def test_walk_share_estimates_unknown_sizes():
    """share-list 无单文件 size → 按总大小/文件数均摊估算，size_unknown 解除。"""

    async def scenario():
        info = {"fileSize": 388029770207, "pwd_id": "", "stoken": "s", "receive_code": ""}
        calls: list[str] = []

        async def fake_share_list(share_code, pdir_fid="", pwd_id="", stoken="", receive_code=""):
            calls.append(pdir_fid)
            if pdir_fid == "":
                return {"list": [
                    {"fileName": "F 根目录", "fileId": "root", "fileIdToken": "t", "isFolder": "True"},
                ]}
            return {"list": [
                {"fileName": "190.mkv", "fileId": "f190", "fileIdToken": "t190", "isFolder": "False"},
                {"fileName": "189.mkv", "fileId": "f189", "fileIdToken": "t189", "isFolder": "False"},
            ]}

        with patch("app.tasks.scan.cloudsaver.share_list", new=fake_share_list):
            files = await _walk_share("code", info, max_depth=2)

        assert len(files) == 2
        expected = max(1, 388029770207 // 2)  # ≈1.9GB/集
        assert all(f["file_size"] == expected for f in files)
        assert all(f["size_unknown"] is False for f in files)
        assert all(f["size_estimated"] is True for f in files)
        assert files[0]["file_name"] == "190.mkv"  # 匹配 S01E190 的文件不再被虚报大小

    asyncio.run(scenario())


def test_walk_share_no_share_size_keeps_fail_closed():
    """share-info 无总大小 → 无法估算 → 保持 size_unknown=True（fail-closed 语义不变）。"""

    async def scenario():
        info = {"pwd_id": "", "stoken": "s"}

        async def fake_share_list(share_code, pdir_fid="", pwd_id="", stoken="", receive_code=""):
            if pdir_fid == "":
                return {"list": [{"fileName": "根", "fileId": "r", "fileIdToken": "t", "isFolder": "True"}]}
            return {"list": [{"fileName": "190.mkv", "fileId": "f", "fileIdToken": "t", "isFolder": "False"}]}

        with patch("app.tasks.scan.cloudsaver.share_list", new=fake_share_list):
            files = await _walk_share("code", info, max_depth=2)

        assert len(files) == 1
        assert files[0]["size_unknown"] is True
        assert files[0]["file_size"] == 0
        assert "size_estimated" not in files[0]

    asyncio.run(scenario())


def test_walk_share_single_file_share_estimates_to_total():
    """单文件分享（顶层面板即文件）：估算退化为总大小（share_size // 1），行为合理。"""

    async def scenario():
        info = {"fileSize": 2147483648, "pwd_id": "", "stoken": "s", "receive_code": ""}

        async def fake_share_list(share_code, pdir_fid="", pwd_id="", stoken="", receive_code=""):
            return {"list": [
                {"fileName": "movie.mkv", "fileId": "f1", "fileIdToken": "t1", "isFolder": "False"},
            ]}

        with patch("app.tasks.scan.cloudsaver.share_list", new=fake_share_list):
            files = await _walk_share("code", info, max_depth=2)

        assert len(files) == 1
        assert files[0]["file_size"] == 2147483648
        assert files[0]["size_unknown"] is False

    asyncio.run(scenario())