"""P0-4：delete_media 外键删除顺序 + 进行中任务前置检查回归测试。

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向临时目录（app.database
模块级 engine 创建时读取），使用独立临时 SQLite，不碰生产数据。
lifespan 由 TestClient 上下文自动触发；scheduler 各 job 注册即 paused
（阶段 3 默认关闭），测试期间不会有定时任务并发消费 pending 数据。
"""
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_delmedia_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 隔离外部服务：避免冒烟测试发起真实外部网络调用
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_USERNAME"] = ""
os.environ["CLOUDSAVER_PASSWORD"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""
os.environ["ALIST_BASE_URL"] = ""
os.environ["ALIST_TOKEN"] = ""
os.environ["ARIA2_RPC_URL"] = ""
os.environ["ARIA2_TOKEN"] = ""
os.environ["NASTOOLS_BASE_URL"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。

    注意：全量 pytest 运行时本模块可能与 test_api_smoke 共享同一 SQLite 引擎
    （引擎为模块级单例，本模块导入时的 DATA_DIR 覆盖对已初始化的引擎无效），
    smoke 测试残留的 watch_requests / invites / notifications 均引用 users.id
    → 直接 delete admin 会触发外键约束失败。故先清空这些子表再删 admin。
    """
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import InviteCode, Notification, User, WatchRequest
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        # 清空引用 users.id 的全部子表（FK：watch_requests.requested_by/reviewed_by、
        # invites.created_by/used_by、notifications.recipient）
        for model in (Notification, InviteCode, WatchRequest):
            await session.execute(delete(model))
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None
    return password


async def _seed_media():
    """注入多组数据（同事件循环），覆盖 FK 链与各进行中态拦截场景。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import DownloadTask, EpisodeState, Media, TransferQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        # A：FK 链全终态——download_task.transfer_id 引用 transfer_queue.id，
        #    旧删除顺序（transfer_queue 先删）会触发 FOREIGN KEY constraint failed。
        media_a = Media(title="FK链删除测试", status="tracking", in_emby=False)
        session.add(media_a)
        await session.flush()
        tq_a = TransferQueue(
            media_id=media_a.id, episode="S01E01", file_name="f01.mkv",
            file_size=100, share_code="ScAa1111", status="done", updated_at=now,
        )
        session.add(tq_a)
        await session.flush()
        session.add(
            DownloadTask(
                media_id=media_a.id, transfer_id=tq_a.id, episode="S01E01",
                file_name="f01.mkv", aria2_gid="gid-a", status="complete",
                local_path="/downloads/f01.mkv", downloaded_at=now,
            )
        )

        # B：transfer_queue 有 pending → 删除应被 409 拦截
        media_b = Media(title="TQ进行中拦截", status="tracking", in_emby=False)
        session.add(media_b)
        await session.flush()
        session.add(
            TransferQueue(
                media_id=media_b.id, episode="S01E01", file_name="f01.mkv",
                file_size=100, share_code="ScBb2222", status="pending", updated_at=now,
            )
        )

        # C：episode_state 有 queued（无 transfer_queue）→ 409
        media_c = Media(title="ES进行中拦截", status="tracking", in_emby=False)
        session.add(media_c)
        await session.flush()
        session.add(
            EpisodeState(
                media_id=media_c.id, episode="S01E01", state="queued",
                file_name="f01.mkv", file_size=100, retry_count=0, updated_at=now,
            )
        )

        # D：download_task 有 downloading → 409
        media_d = Media(title="DT进行中拦截", status="tracking", in_emby=False)
        session.add(media_d)
        await session.flush()
        session.add(
            DownloadTask(
                media_id=media_d.id, transfer_id=None, episode="S01E01",
                file_name="f01.mkv", aria2_gid="gid-d", status="downloading",
            )
        )

        # E：无任何子表 → 删除应成功
        media_e = Media(title="干净删除", status="tracking", in_emby=False)
        session.add(media_e)

        await session.commit()
        return {
            "fk_media_id": media_a.id,
            "tq_pending_media_id": media_b.id,
            "es_queued_media_id": media_c.id,
            "dt_downloading_media_id": media_d.id,
            "clean_media_id": media_e.id,
        }


def test_delete_media_fk_order_and_in_progress_guard():
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        admin_tok = r.json()["access_token"]
        h = _auth(admin_tok)

        seed = client.portal.call(_seed_media)

        # 进行中任务 → 409（transfer_queue pending / episode_state queued / download_task downloading）
        for mid in (
            seed["tq_pending_media_id"],
            seed["es_queued_media_id"],
            seed["dt_downloading_media_id"],
        ):
            r = client.delete(f"/api/media/{mid}", headers=h)
            assert r.status_code == 409, r.text
            assert "进行中" in r.json()["detail"]
            # 409 拦截后数据仍在（未误删）
            assert client.get(f"/api/media/{mid}", headers=h).status_code == 200

        # FK 链删除（download_task 引用 transfer_queue）→ 成功（旧顺序会外键冲突 500）
        r = client.delete(f"/api/media/{seed['fk_media_id']}", headers=h)
        assert r.status_code == 200, r.text
        assert client.get(f"/api/media/{seed['fk_media_id']}", headers=h).status_code == 404

        # 干净删除（无子表）→ 成功
        r = client.delete(f"/api/media/{seed['clean_media_id']}", headers=h)
        assert r.status_code == 200, r.text

        # 不存在 → 404
        assert client.delete("/api/media/99999", headers=h).status_code == 404

        # 未登录 → 401
        assert client.delete(f"/api/media/{seed['tq_pending_media_id']}").status_code == 401


async def _seed_pause_guard():
    """B-1：进行中（download_task downloading）与良性终态（无任何子表）media 各一。"""
    from app.database import async_session
    from app.models import DownloadTask, Media

    async with async_session() as session:
        busy = Media(title="B1暂停拦截", status="tracking", in_emby=False)
        session.add(busy)
        await session.flush()
        session.add(
            DownloadTask(
                media_id=busy.id, transfer_id=None, episode="S01E01",
                file_name="f01.mkv", aria2_gid="gid-b1", status="downloading",
            )
        )
        idle = Media(title="B1良性终态", status="tracking", in_emby=False)
        session.add(idle)
        await session.commit()
        return {"busy_media_id": busy.id, "idle_media_id": idle.id}


def test_patch_paused_rejects_in_progress_and_allows_idle():
    """B-1：进行中任务存在时设 paused → 409（状态不落库）；无进行中任务 → 200 且生效；tracking 不受限。"""
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        h = _auth(r.json()["access_token"])

        ids = client.portal.call(_seed_pause_guard)

        # 进行中任务（download_task downloading）→ 设 paused 被 409 拦截
        r = client.patch(
            f"/api/media/{ids['busy_media_id']}", json={"status": "paused"}, headers=h
        )
        assert r.status_code == 409, r.text
        assert "进行中" in r.json()["detail"]
        # 拦截后 status 未被改动（仍 tracking）
        r = client.get(f"/api/media/{ids['busy_media_id']}", headers=h)
        assert r.json()["status"] == "tracking"

        # 设置 tracking 不受进行中任务限制
        r = client.patch(
            f"/api/media/{ids['busy_media_id']}", json={"status": "tracking"}, headers=h
        )
        assert r.status_code == 200 and r.json()["status"] == "tracking"

        # 良性终态（无任何子表）→ paused 允许设置且落库
        r = client.patch(
            f"/api/media/{ids['idle_media_id']}", json={"status": "paused"}, headers=h
        )
        assert r.status_code == 200 and r.json()["status"] == "paused"
        # 恢复 tracking 同样不受限
        r = client.patch(
            f"/api/media/{ids['idle_media_id']}", json={"status": "tracking"}, headers=h
        )
        assert r.status_code == 200 and r.json()["status"] == "tracking"


def test_delete_media_acquires_scan_per_media_lock(monkeypatch):
    """B-2：delete_media 的「检查+删除+commit」在 scan 的 per-media 锁内执行。

    以「计数 + 转发原函数」包装 _media_lock（delete_media 在请求内延迟 import，
    from ... import 为运行时属性查找，monkeypatch 即时生效），走 TestClient
    正常删除干净 media，断言删除成功且锁被获取。
    """
    from app.tasks import scan as scan_mod

    original = scan_mod._media_lock
    calls: list[int] = []

    def counting_lock(media_id: int):
        calls.append(media_id)
        return original(media_id)

    monkeypatch.setattr(scan_mod, "_media_lock", counting_lock)

    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        h = _auth(r.json()["access_token"])

        # 复用 _seed_media 中「无任何子表」的干净 media（E）
        mid = client.portal.call(_seed_media)["clean_media_id"]
        r = client.delete(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        assert client.get(f"/api/media/{mid}", headers=h).status_code == 404
        assert mid in calls  # 删除流程确实持 scan 的 per-media 锁执行
