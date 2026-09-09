"""queue 列表接口契约测试：分页 total、创建时间升序、分享码脱敏、share_url 构造。

基建：完全参照 backend/tests/test_media_two_queue.py —— 模块导入前把
LUMENCLOUD_DATA_DIR 指向临时目录 + 隔离全部外部服务环境变量 +
TestClient(app) + 登录 admin/guest 拿 token（_auth helper）+
async_session 直接 seed 数据（同事件循环 portal.call）。

覆盖（Task 1 契约）：
1. GET /queue 返回 {"items": [...], "total": N}：limit 只约束 items 长度，total 为全量数
2. _list_flat 按创建时间升序（TQ created_at=DQ enqueued_at 键），同时间 id 升序
3. _list_download（?type=download）按 enqueued_at 升序、同时间 id 升序
4. share_code 仅 admin 明文、guest 为 null（flat 与 download 两种都验证）
5. share_url 由 share_code 构造 https://pan.quark.cn/s/<code>；无分享码为 null
6. size_estimated 键输出（Task 2 落库前均为 False，先固定契约键存在）
"""
import itertools
import os
import tempfile
from datetime import datetime, timedelta, timezone

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_qlist_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 隔离外部服务：避免测试发起真实外部网络调用
for _k in (
    "TMDB_API_KEY",
    "TMDB_PROXY",
    "CLOUDSAVER_BASE_URL",
    "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD",
    "EMBY_BASE_URL",
    "EMBY_API_KEY",
    "ALIST_BASE_URL",
    "ALIST_TOKEN",
    "ARIA2_RPC_URL",
    "ARIA2_TOKEN",
    "NASTOOLS_BASE_URL",
    "PUSHPLUS_TOKEN",
):
    os.environ[_k] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

_GUEST_SEQ = itertools.count(1)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _now() -> datetime:
    """naive UTC 当前时间（与项目 stored naive UTC 口径一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _clear_queue_tables():
    """每个用例前清空两队列表，保证 total/排序断言不受前序用例残留影响。"""
    from sqlalchemy import delete  # noqa: PLC0415

    from app.database import async_session  # noqa: PLC0415
    from app.models import DownloadQueue, TaskQueue  # noqa: PLC0415

    async with async_session() as s:
        await s.execute(delete(DownloadQueue))
        await s.execute(delete(TaskQueue))
        await s.commit()


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码。"""
    from sqlalchemy import delete  # noqa: PLC0415

    from app.database import async_session  # noqa: PLC0415
    from app.models import InviteCode, Notification, User, WatchRequest  # noqa: PLC0415
    from app.routers.auth import ensure_admin  # noqa: PLC0415

    async with async_session() as s:
        for model in (Notification, InviteCode, WatchRequest):
            await s.execute(delete(model))
        await s.execute(delete(User).where(User.role == "admin"))
        await s.commit()
    password = await ensure_admin()
    assert password is not None
    return password


def _login_admin(client) -> str:
    admin_password = client.portal.call(_recreate_admin)
    r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _login_guest(client, admin_tok: str) -> str:
    r = client.post("/api/admin/invites", json={"count": 1}, headers=_auth(admin_tok))
    assert r.status_code == 200, r.text
    invite = r.json()["codes"][0]
    username = f"gl{next(_GUEST_SEQ)}"
    r = client.post(
        "/api/auth/register",
        json={"username": username, "password": "pass1234", "invite_code": invite},
    )
    assert r.status_code == 200, r.text
    return client.post(
        "/api/auth/login", json={"username": username, "password": "pass1234"}
    ).json()["access_token"]


# ---------------------------------------------------------------------------
# seed helpers（portal.call 仅支持位置参数，统一位置签名）
# ---------------------------------------------------------------------------

async def _seed_media(title: str = "队列契约影视") -> int:
    from app.database import async_session  # noqa: PLC0415
    from app.models import Media  # noqa: PLC0415

    async with async_session() as s:
        media = Media(title=title, status="tracking", in_emby=False)
        s.add(media)
        await s.flush()
        await s.commit()
        return media.id


async def _seed_dq(mid, episode, enqueued_at, share_code="AbCd1234XyZq", status="pending"):
    from app.database import async_session  # noqa: PLC0415
    from app.models import DownloadQueue  # noqa: PLC0415

    async with async_session() as s:
        dq = DownloadQueue(
            media_id=mid, episode=episode, file_name=f"{episode}.mkv", file_size=1024,
            share_code=share_code, stoken="st", receive_code="rc", fids="[]",
            fid_tokens="[]", folder_id="fd", status=status,
            enqueued_at=enqueued_at, updated_at=_now(),
        )
        s.add(dq)
        await s.flush()
        await s.commit()
        return dq.id


async def _seed_tq(mid, episode, created_at, share_code="TqXxYyZz1234"):
    from app.database import async_session  # noqa: PLC0415
    from app.models import TaskQueue  # noqa: PLC0415

    async with async_session() as s:
        tq = TaskQueue(
            media_id=mid, episode=episode, file_name=f"{episode}.mkv", file_size=1024,
            share_code=share_code, pwd_id="pwd", stoken="st", receive_code="rc",
            fids="[]", fid_tokens="[]", folder_id="fd", status="ready",
            probe_attempt=1, created_at=created_at, updated_at=_now(),
        )
        s.add(tq)
        await s.flush()
        await s.commit()
        return tq.id


# ---------------------------------------------------------------------------
# 1. 分页契约：{items, total}
# ---------------------------------------------------------------------------

def test_list_queue_returns_items_and_total():
    """GET /queue → {items,total}：limit 仅约束 items 长度，total 为全量活跃数。"""
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        base = _now()
        for i in range(3):
            client.portal.call(_seed_dq, mid, f"S01E0{i + 1}", base + timedelta(minutes=i))

        r = client.get("/api/queue", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, dict) and set(body) == {"items", "total"}
        assert body["total"] == 3 and len(body["items"]) == 3

        r1 = client.get("/api/queue", params={"limit": 1}, headers=_auth(admin_tok))
        b1 = r1.json()
        assert b1["total"] == 3 and len(b1["items"]) == 1

        r2 = client.get("/api/queue", params={"limit": 2, "offset": 1}, headers=_auth(admin_tok))
        b2 = r2.json()
        assert b2["total"] == 3 and len(b2["items"]) == 2


# ---------------------------------------------------------------------------
# 2. _list_flat 创建时间升序（TQ created_at=DQ enqueued_at 统一键；同时间 id 升序）
# ---------------------------------------------------------------------------

def test_list_flat_sorts_by_created_asc_and_id_asc():
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        base = _now()
        # TQ 两行同 created_at（验证同时间 id 升序）+ 一行更晚 TQ + 一行 DQ（enqueued_at）
        client.portal.call(_seed_tq, mid, "S01A", base - timedelta(minutes=6), "TqXxYyZz1411")
        client.portal.call(_seed_tq, mid, "S01B", base - timedelta(minutes=6), "TqXxYyZz1512")
        client.portal.call(_seed_tq, mid, "S01C", base - timedelta(minutes=4), "TqXxYyZz1613")
        client.portal.call(_seed_dq, mid, "S01D", base - timedelta(minutes=2), "AbCd1234XyZq")

        r = client.get("/api/queue", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 4
        episodes = [x["episode"] for x in body["items"]]
        # 创建时间升序：A/B(同刻, id 升序) → C → D
        assert episodes == ["S01A", "S01B", "S01C", "S01D"]
        # 每行携带 enqueued_at（排序键输出，前端可读）
        assert all(x["enqueued_at"] is not None for x in body["items"])


# ---------------------------------------------------------------------------
# 3. _list_download（?type=download）enqueued_at 升序
# ---------------------------------------------------------------------------

def test_list_download_sorts_by_enqueued_asc():
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        base = _now()
        client.portal.call(_seed_dq, mid, "S01E03", base + timedelta(minutes=5))
        client.portal.call(_seed_dq, mid, "S01E01", base)
        client.portal.call(_seed_dq, mid, "S01E02", base + timedelta(minutes=2))

        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, dict) and set(body) == {"items", "total"}
        assert body["total"] == 3
        assert [x["episode"] for x in body["items"]] == ["S01E01", "S01E02", "S01E03"]


def test_list_download_same_time_id_asc():
    """同 enqueued_at → id 升序决胜。"""
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        base = _now()
        dq_first = client.portal.call(_seed_dq, mid, "S01E01", base)
        dq_second = client.portal.call(_seed_dq, mid, "S01E02", base)

        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(admin_tok))
        body = r.json()
        ids = [x["id"] for x in body["items"]]
        assert ids == [dq_first, dq_second]


# ---------------------------------------------------------------------------
# 4. share_code 脱敏：admin 明文，guest null（flat + download）
# ---------------------------------------------------------------------------

def test_share_code_admin_plain_guest_hidden():
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        guest_tok = _login_guest(client, admin_tok)
        mid = client.portal.call(_seed_media)
        client.portal.call(_seed_dq, mid, "S01E01", _now(), "AbCd1234XyZq")
        client.portal.call(_seed_tq, mid, "S01E02", _now(), "TqXxYyZz1234")

        # admin 扁平：share_code 明文
        r = client.get("/api/queue", headers=_auth(admin_tok))
        body = r.json()
        by_ep = {x["episode"]: x for x in body["items"]}
        assert by_ep["S01E01"]["share_code"] == "AbCd1234XyZq"
        assert by_ep["S01E02"]["share_code"] == "TqXxYyZz1234"

        # guest 扁平：share_code 全部 null（不泄露凭据）
        r = client.get("/api/queue", headers=_auth(guest_tok))
        body = r.json()
        assert all(x["share_code"] is None for x in body["items"])

        # download 视图：admin 明文 / guest null
        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(admin_tok))
        d = r.json()
        assert next(x for x in d["items"] if x["episode"] == "S01E01")["share_code"] == "AbCd1234XyZq"
        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(guest_tok))
        d = r.json()
        assert all(x["share_code"] is None for x in d["items"])


# ---------------------------------------------------------------------------
# 5. share_url 构造（有/无分享码）
# ---------------------------------------------------------------------------

def test_share_url_constructed():
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        client.portal.call(_seed_dq, mid, "S01E01", _now(), "AbCd1234XyZq")
        client.portal.call(_seed_dq, mid, "S01E02", _now(), "")  # 无分享码

        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(admin_tok))
        body = r.json()
        by_ep = {x["episode"]: x for x in body["items"]}
        assert by_ep["S01E01"]["share_url"] == "https://pan.quark.cn/s/AbCd1234XyZq"
        assert by_ep["S01E02"]["share_url"] is None


# ---------------------------------------------------------------------------
# 6. size_estimated 契约键（Task 2 落库前恒 False，先固定输出键存在）
# ---------------------------------------------------------------------------

def test_list_rows_contain_size_estimated_key():
    with TestClient(app) as client:
        client.portal.call(_clear_queue_tables)
        admin_tok = _login_admin(client)
        mid = client.portal.call(_seed_media)
        client.portal.call(_seed_dq, mid, "S01E01", _now(), "AbCd1234XyZq")
        client.portal.call(_seed_tq, mid, "S01E02", _now(), "TqXxYyZz1234")

        r = client.get("/api/queue", headers=_auth(admin_tok))
        body = r.json()
        assert all("size_estimated" in x and x["size_estimated"] is False for x in body["items"])

        r = client.get("/api/queue", params={"type": "download"}, headers=_auth(admin_tok))
        body = r.json()
        assert all("size_estimated" in x and x["size_estimated"] is False for x in body["items"])