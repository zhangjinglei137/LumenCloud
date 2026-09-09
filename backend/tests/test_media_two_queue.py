"""media 路由两队列重构遗留问题回归测试。

覆盖三个缺陷修复：
1. _has_in_progress_tasks 新表检查（TaskQueue/DownloadQueue）——download_queue
   进行中行阻断 DELETE 与 PATCH paused；
2. delete_media 补充新表删除（download_queue → task_queue 顺序）——全终态删除成功
   且两新表+旧表均无残留（直接查库断言）；
3. list_media/get_media 读新表为主、旧表兼容——统计口径三表去重合并；
   详情 episode_state 以 download_queue 行为主（share_code 脱敏），transfer_queue
   以 task_queue 行为主；同 (media, episode) 新旧表同时存在时只出现一次（去重）。

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向临时目录，使用独立临时 SQLite，
不碰生产数据。lifespan 由 TestClient 上下文自动触发。
"""
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_media2q_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# 隔离外部服务：避免测试发起真实外部网络调用
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

import pytest  # noqa: E402
from datetime import date  # noqa: E402

from app.main import app  # noqa: E402
from app.routers.media import resolve_episode_status  # noqa: E402


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。

    全量 pytest 运行时本模块可能与 test_api_smoke 共享同一 SQLite 引擎（引擎为
    模块级单例），smoke 残留的 watch_requests / invites / notifications 均引用
    users.id → 直接 delete admin 会触发外键约束失败。故先清空这些子表再删 admin。
    """
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import InviteCode, Notification, User, WatchRequest
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        for model in (Notification, InviteCode, WatchRequest):
            await session.execute(delete(model))
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None
    return password


async def _seed_dq_in_progress():
    """场景 1：media 仅含 download_queue('transferring') 进行中行。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import DownloadQueue, Media

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="DQ进行中拦截", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=100, share_code="ScAa1111", status="transferring",
                enqueued_at=now, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _seed_terminal_two_queue():
    """场景 2：media 仅含 download_queue('done') + task_queue('done') 全终态行。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="两队列终态删除", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=100, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        session.add(
            TaskQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=100, status="done", probe_attempt=1, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _seed_stats_and_detail():
    """场景 3：dq(done S01E01) + tq(done S01E02)，供列表统计 + 详情脱敏断言。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import DownloadQueue, Media, TaskQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="新表统计影视", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1500000000, share_code="AbCd1234XyZq", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        session.add(
            TaskQueue(
                media_id=media.id, episode="S01E02", file_name="f02.mkv",
                file_size=99, status="done", probe_attempt=1, updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _seed_dedup_merge():
    """场景 4：同 episode 新旧表行并存（episode_state/dq 同键 + transfer_queue/tq 同键），
    另有仅旧表遗留行验证合并逻辑（es S01E03 / 旧 transfer_queue S01E04）。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import DownloadQueue, EpisodeState, Media, TaskQueue, TransferQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="新旧表去重影视", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()
        # S01E01：新表 dq + 旧表 es 同键 → 详情 episode_state 只出现一次（dqdto）
        session.add(
            DownloadQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=100, share_code="ScAa1111", status="done",
                enqueued_at=now, updated_at=now,
            )
        )
        session.add(
            EpisodeState(
                media_id=media.id, episode="S01E01", state="done",
                file_name="f01.mkv", file_size=100, share_code="ScAa1111",
                retry_count=0, updated_at=now,
            )
        )
        # S01E02：新表 tq + 旧表 transfer_queue 同键 → 详情 transfer_queue 只出现一次（tq dto）
        session.add(
            TaskQueue(
                media_id=media.id, episode="S01E02", file_name="f02.mkv",
                file_size=200, status="ready", probe_attempt=1, updated_at=now,
            )
        )
        session.add(
            TransferQueue(
                media_id=media.id, episode="S01E02", file_name="f02.mkv",
                file_size=200, share_code="ScBb2222", status="pending", updated_at=now,
            )
        )
        # S01E03：仅旧表 episode_state（遗留行，无 dq）→ 详情以旧 DTO 合并展示
        session.add(
            EpisodeState(
                media_id=media.id, episode="S01E03", state="failed",
                file_name="f03.mkv", file_size=300, share_code="Qq7Ww8ZzNm1p",
                retry_count=3, error="确定性失败", updated_at=now,
            )
        )
        # S01E04：仅旧表 transfer_queue（遗留行，无 tq）→ 详情以旧 DTO 合并展示
        session.add(
            TransferQueue(
                media_id=media.id, episode="S01E04", file_name="f04.mkv",
                file_size=400, share_code="ScCc3333", status="done", updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id}


async def _count_child_rows(media_id: int) -> dict[str, int]:
    """直接查库统计 media 的全部子表行数（两新表 + 旧三表）。"""
    from sqlalchemy import func, select

    from app.database import async_session
    from app.models import (
        DownloadQueue,
        DownloadTask,
        EpisodeState,
        TaskQueue,
        TransferQueue,
    )

    async with async_session() as session:
        result: dict[str, int] = {}
        for model in (EpisodeState, DownloadTask, TransferQueue, DownloadQueue, TaskQueue):
            n = await session.scalar(
                select(func.count()).select_from(model).where(model.media_id == media_id)
            )
            result[model.__tablename__] = n or 0
        return result


def test_delete_and_patch_rejected_when_download_queue_in_progress():
    """缺陷 1：media 含 download_queue('transferring') → DELETE 409；PATCH paused 409。"""
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        h = _auth(r.json()["access_token"])

        mid = client.portal.call(_seed_dq_in_progress)["media_id"]

        # DELETE → 409（新表进行中拦截），数据仍在
        r = client.delete(f"/api/media/{mid}", headers=h)
        assert r.status_code == 409, r.text
        assert "进行中" in r.json()["detail"]
        assert client.get(f"/api/media/{mid}", headers=h).status_code == 200

        # PATCH paused → 409
        r = client.patch(f"/api/media/{mid}", json={"status": "paused"}, headers=h)
        assert r.status_code == 409, r.text
        assert "进行中" in r.json()["detail"]
        # 拦截后 status 未被改动
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200 and r.json()["status"] == "tracking"

        # 恢复 tracking 不受限制
        r = client.patch(f"/api/media/{mid}", json={"status": "tracking"}, headers=h)
        assert r.status_code == 200 and r.json()["status"] == "tracking"


def test_delete_terminal_two_queue_removes_all_child_rows():
    """缺陷 2：download_queue('done') + task_queue('done') 全终态 → DELETE 成功，
    两新表与旧三表均无残留（直接查库断言）。"""
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        h = _auth(r.json()["access_token"])

        mid = client.portal.call(_seed_terminal_two_queue)["media_id"]

        # 删除前：新表两行各 1（旧表 0）
        before = client.portal.call(_count_child_rows, mid)
        assert before["download_queue"] == 1 and before["task_queue"] == 1

        r = client.delete(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        assert client.get(f"/api/media/{mid}", headers=h).status_code == 404

        # 删除后：两新表与旧三表全部无残留
        after = client.portal.call(_count_child_rows, mid)
        assert after == {
            "episode_state": 0,
            "download_task": 0,
            "transfer_queue": 0,
            "download_queue": 0,
            "task_queue": 0,
        }, after


def test_list_stats_and_detail_read_new_tables_with_masking():
    """缺陷 3：list_media 统计读新表（episode_stats.done 正确）；详情 episode_state
    含 DownloadQueue 行且 share_code 脱敏（admin 看到 ****末4位，guest 不返回）。"""
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        admin_tok = r.json()["access_token"]

        # guest 注册 + 登录（脱敏分级断言用）
        r = client.post("/api/admin/invites", json={"count": 1}, headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        invite = r.json()["codes"][0]
        r = client.post(
            "/api/auth/register",
            json={"username": "guest2q", "password": "pass1234", "invite_code": invite},
        )
        assert r.status_code == 200, r.text
        guest_tok = client.post(
            "/api/auth/login", json={"username": "guest2q", "password": "pass1234"}
        ).json()["access_token"]

        mid = client.portal.call(_seed_stats_and_detail)["media_id"]

        # 列表统计：dq done(S01E01) + tq done(S01E02) → total=2, done=1
        r = client.get("/api/media", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        m = next(x for x in r.json() if x["id"] == mid)
        assert m["episode_stats"] == {
            "total": 2, "done": 1, "failed": 0, "in_progress": 0,
            "available": 1, "downloaded": 1, "missing": 1,
        }
        assert m["episode_state"] == {
            "total": 2, "done": 1, "failed": 0, "in_progress": 0,
        }

        # 详情：episode_state 主数据源 = download_queue 行（state=status 别名）
        r = client.get(f"/api/media/{mid}", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert set(by_ep) == {"S01E01"}  # tq 行进 transfer_queue，不进 episode_state
        dq_row = by_ep["S01E01"]
        assert dq_row["status"] == "done" and dq_row["state"] == "done"
        assert dq_row["file_name"] == "f01.mkv"
        # admin 脱敏：share_code 仅后 4 位
        assert dq_row["share_code"] == "****XyZq"
        # transfer_queue 主数据源 = task_queue 行
        tq_by_ep = {t["episode"]: t for t in r.json()["transfer_queue"]}
        assert set(tq_by_ep) == {"S01E02"}
        assert tq_by_ep["S01E02"]["status"] == "done"
        assert "media_id" in tq_by_ep["S01E02"] and "created_at" in tq_by_ep["S01E02"]

        # guest：episode_state 不返回任何凭据字段
        r = client.get(f"/api/media/{mid}", headers=_auth(guest_tok))
        assert r.status_code == 200, r.text
        assert all(
            "share_code" not in e and "aria2_gid" not in e and "quark_path" not in e
            for e in r.json()["episode_state"]
        )
        assert all("share_code" not in t for t in r.json()["transfer_queue"])


def test_detail_dedup_when_new_and_old_rows_share_episode():
    """缺陷 3 去重：同 (media, episode) 新表与旧表行并存时详情只出现一次；
    仅旧表遗留行（无新表同键）仍以旧 DTO 合并展示。"""
    with TestClient(app) as client:
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        h = _auth(r.json()["access_token"])

        mid = client.portal.call(_seed_dedup_merge)["media_id"]
        r = client.get(f"/api/media/{mid}", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()

        eps = body["episode_state"]
        ep_names = [e["episode"] for e in eps]
        tqs = body["transfer_queue"]
        tq_names = [t["episode"] for t in tqs]

        # S01E01：dq + 旧 es 同键 → 只出现一次，且为 download_queue 行（status 别名）
        assert ep_names.count("S01E01") == 1
        e01 = next(e for e in eps if e["episode"] == "S01E01")
        assert e01["status"] == "done" and e01["id"] > 0

        # S01E02：tq + 旧 transfer_queue 同键 → transfer_queue 只出现一次，且为 task_queue 行
        assert tq_names.count("S01E02") == 1
        t02 = next(t for t in tqs if t["episode"] == "S01E02")
        assert t02["status"] == "ready"

        # 仅旧表遗留行仍合并展示（无新表同键）
        assert "S01E03" in ep_names  # 旧 episode_state 遗留
        e03 = next(e for e in eps if e["episode"] == "S01E03")
        assert e03["status"] == "failed" and e03["share_code"] == "****Nm1p"
        assert "S01E04" in tq_names  # 旧 transfer_queue 遗留
        t04 = next(t for t in tqs if t["episode"] == "S01E04")
        assert t04["status"] == "done" and t04["share_code"] == "****3333"


# ---------------------------------------------------------------------------
# episode-status-cache Task 4：resolve_episode_status 纯函数（5 态状态机）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("local,in_emby,air_date,today,expected", [
    ("done", False, "2026-01-01", date(2026, 9, 9), "in_library"),
    (None, True, None, date(2026, 9, 9), "in_library"),           # Emby 收录即已在库
    ("failed", False, "2026-01-01", date(2026, 9, 9), "error"),   # 开播但失败
    ("unmatched", False, "2026-01-01", date(2026, 9, 9), "error"),
    ("queued", False, "2026-01-01", date(2026, 9, 9), "scanning"),# 巡检/下载中
    ("downloading", False, "2026-01-01", date(2026, 9, 9), "scanning"),
    (None, False, "2027-01-01", date(2026, 9, 9), "not_aired"),   # 未开播
    (None, False, None, date(2026, 9, 9), "pending"),             # 待定
])
def test_resolve_episode_status(local, in_emby, air_date, today, expected):
    assert resolve_episode_status(local, in_emby, air_date, today) == expected


def test_resolve_episode_status_error_over_scanning():
    # 同一集既有 failed 又有 queued → 异常优先于巡检中
    assert resolve_episode_status("failed", False, "2026-01-01", date(2026, 9, 9)) == "error"
