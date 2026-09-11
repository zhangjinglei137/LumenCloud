"""安全审查 D7：GET /api/queue?type=download 对 guest 脱敏 aria2_gid（Medium）。

线上风险:queue.py _list_download 的 DTO 中 `"aria2_gid": dq.aria2_gid` 对
所有角色明文返回——与 media.py §9.1 脱敏口径不一致（媒体详情/剧集 DTO 明确
「share_code/aria2_gid/quark_path guest 不返回，admin 才可见」）。aria2_gid
是 aria2 下载任务 ID，guest 明文可见扩大敏感执行面（配合 aria2 RPC 可定位/操作
下载任务）。

TDD：先写本测试（RED：修复前 guest 响应中 aria2_gid 为明文），再实现脱敏（GREEN）。
"""
import os
import tempfile
import uuid

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_aria2_gid_")
os.environ.setdefault("LUMENCLOUD_DATA_DIR", _TMP_DATA)
for _K in (
    "TMDB_API_KEY", "TMDB_PROXY", "CLOUDSAVER_BASE_URL", "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD", "EMBY_BASE_URL", "EMBY_API_KEY", "ALIST_BASE_URL",
    "ALIST_TOKEN", "ARIA2_RPC_URL", "ARIA2_TOKEN", "NASTOOLS_BASE_URL", "PUSHPLUS_TOKEN",
):
    os.environ[_K] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed() -> dict:
    """seed admin/guest + media + 活跃 download_queue 行（含 aria2_gid / share_code）。

    file_name 用 uuid 唯一化——全量回归与其他测试共享同一 app/DB，队列中可能
    存在其它测试残留的 download_queue 活跃行，断言改为「按唯一 file_name 查找
    自 seed 行」，与执行顺序/残留数据鲁棒。
    """
    from app.database import async_session
    from app.models import DownloadQueue, Media, User
    from app.routers.auth import create_access_token, hash_password

    uniq = uuid.uuid4().hex[:8]
    file_name = f"ep-{uniq}.mkv"
    async with async_session() as session:
        admin = User(
            username=f"admin_{uuid.uuid4().hex[:10]}",
            password_hash=hash_password("x"),
            role="admin",
        )
        guest = User(
            username=f"guest_{uuid.uuid4().hex[:10]}",
            password_hash=hash_password("x"),
            role="guest",
        )
        media = Media(title=f"Media {uniq}", media_type="tv")
        session.add_all([admin, guest, media])
        await session.flush()
        dq = DownloadQueue(
            media_id=media.id,
            episode="S01E01",
            file_name=file_name,
            file_size=1024,
            share_code="SHARECODE123456",
            status="downloading",
            aria2_gid="gid-12345-secret",
        )
        session.add(dq)
        await session.flush()
        toks = {
            "admin_tok": create_access_token(admin),
            "guest_tok": create_access_token(guest),
        }
        await session.commit()
        return {**toks, "file_name": file_name}


@pytest.fixture(scope="module")
def users():
    with TestClient(app) as client:
        seeded = client.portal.call(_seed)
        yield {"client": client, **seeded}


def _download_items(client, tok: str) -> list[dict]:
    r = client.get("/api/queue?type=download", headers=_auth(tok))
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _find_item(items: list[dict], file_name: str) -> dict | None:
    for it in items:
        if it.get("file_name") == file_name:
            return it
    return None


def test_guest_download_list_redacts_aria2_gid(users):
    """guest 列表：aria2_gid / share_code 必须为 None（§9.1 脱敏口径与 media 详情一致）。"""
    item = _find_item(_download_items(users["client"], users["guest_tok"]), users["file_name"])
    assert item is not None, "未找到自 seed 的 download_queue 行"
    assert item["aria2_gid"] is None, item
    assert item["share_code"] is None, item


def test_admin_download_list_sees_aria2_gid(users):
    """admin 列表：aria2_gid / share_code 明文可见（管理排查需要，回归锁定）。"""
    item = _find_item(_download_items(users["client"], users["admin_tok"]), users["file_name"])
    assert item is not None, "未找到自 seed 的 download_queue 行"
    assert item["aria2_gid"] == "gid-12345-secret", item
    assert item["share_code"] == "SHARECODE123456", item