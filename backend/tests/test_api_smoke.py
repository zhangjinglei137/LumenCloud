"""阶段 3 API 层冒烟测试（ASGI 进程内，不启动监听服务）。

数据库隔离：模块导入前把 LUMENCLOUD_DATA_DIR 指向临时目录（app.database
模块级 engine 创建时读取），使用独立临时 SQLite，不碰生产数据。
lifespan（init_db → recover_on_boot → ensure_admin → scheduler）由
TestClient 上下文自动触发。
"""
import os
import tempfile

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_smoke_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP_DATA
# Phase 8：JWT 密钥不再要求 env（自动文件化于 <data_dir>/.jwt_secret）；此处保留
# env 值仅用于 settings 面板 `jwt_secret` 布尔断言（settings.py 检测 env 字段，
# 认证实际使用文件密钥）。
os.environ["JWT_SECRET"] = "smoke-secret-change-me-12345"
os.environ["INIT_ADMIN_USERNAME"] = "admin"
# Phase 8 起 INIT_ADMIN_PASSWORD 弃用：admin 初始密码随机生成，见下方动态登录。
# 隔离外部服务：pydantic-settings 的 settings.env_file 指向项目根 .env，
# 其中含真实凭据，显式置空避免冒烟测试发起真实外部网络调用。
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

# §9.1 网盘凭据字段：任何角色都不应返回
_SENSITIVE_QUEUE_FIELDS = {"stoken", "receive_code", "fid_tokens", "pwd_id", "folder_id", "fids"}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_queue_data():
    """注入一条 pending + 一条 failed 队列数据，供脱敏与 retry 断言（同事件循环）。"""
    from datetime import datetime, timezone

    from app.database import async_session
    from app.models import EpisodeState, Media, TransferQueue

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        media = Media(title="脱敏测试影视", status="tracking", in_emby=False)
        session.add(media)
        await session.flush()

        session.add(
            TransferQueue(
                media_id=media.id, episode="S01E01", file_name="f01.mkv",
                file_size=1500000000, share_code="AbCd1234XyZq", pwd_id="pwd1",
                stoken="stok1", receive_code="rc1", fids="[1]", fid_tokens='["t1"]',
                folder_id="fld1", status="pending", updated_at=now,
            )
        )
        session.add(
            EpisodeState(
                media_id=media.id, episode="S01E01", state="queued",
                file_name="f01.mkv", file_size=1500000000,
                share_code="AbCd1234XyZq", retry_count=0, updated_at=now,
            )
        )

        failed = TransferQueue(
            media_id=media.id, episode="S01E02", file_name="f02.mkv",
            file_size=99, share_code="Qq7Ww8ZzNm1p", status="failed",
            quota_reject_count=0, error="确定性失败", updated_at=now,
        )
        session.add(failed)
        session.add(
            EpisodeState(
                media_id=media.id, episode="S01E02", state="failed",
                file_name="f02.mkv", file_size=99,
                share_code="Qq7Ww8ZzNm1p", retry_count=3, error="确定性失败", updated_at=now,
            )
        )
        await session.commit()
        return {"media_id": media.id, "failed_tq_id": failed.id}


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。

    ensure_admin 首次创建时返回随机初始密码，但 TestClient 无法透传 lifespan 内
    的返回值；此处重建一次以确定性获取初始密码用于登录断言。
    """
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import User
    from app.routers.auth import ensure_admin

    async with async_session() as session:
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()
    password = await ensure_admin()
    assert password is not None  # 已删除 admin，必然重新创建并返回初始密码
    return password


def test_full_auth_and_api_flow():
    with TestClient(app) as client:
        # ---------- 管理员初始化 + 登录（Phase 8：初始密码随机，动态获取） ----------
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        admin_tok = r.json()["access_token"]
        assert r.json()["token_type"] == "bearer"

        # ---------- /auth/me ----------
        r = client.get("/api/auth/me", headers=_auth(admin_tok))
        assert r.status_code == 200 and r.json()["role"] == "admin"

        # 无效 token / 缺 token → 401
        assert client.get("/api/auth/me", headers=_auth("bad.token.here")).status_code == 401
        assert client.get("/api/auth/me").status_code == 401

        # ---------- 邀请码管理 ----------
        r = client.post("/api/admin/invites", json={"count": 2}, headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        codes = r.json()["codes"]
        assert len(codes) == 2

        r = client.get("/api/admin/invites", headers=_auth(admin_tok))
        assert r.status_code == 200 and any(c["code"] == codes[0] for c in r.json())

        # ---------- 邀请码注册 ----------
        r = client.post(
            "/api/auth/register",
            json={"username": "guest1", "password": "pass1234", "invite_code": codes[0]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == "guest"

        # 邀请码复用 → 422
        r = client.post(
            "/api/auth/register",
            json={"username": "guest2", "password": "pass1234", "invite_code": codes[0]},
        )
        assert r.status_code == 422

        # 密码长度 <6 → 422
        r = client.post(
            "/api/auth/register",
            json={"username": "guest3", "password": "123", "invite_code": codes[1]},
        )
        assert r.status_code == 422

        # ---------- guest 登录 ----------
        r = client.post("/api/auth/login", json={"username": "guest1", "password": "pass1234"})
        assert r.status_code == 200, r.text
        guest_tok = r.json()["access_token"]

        # ---------- 注入队列数据（同事件循环）验证 §9.1 脱敏 ----------
        seed = client.portal.call(_seed_queue_data)
        media_id, failed_tq_id = seed["media_id"], seed["failed_tq_id"]

        # guest 视图：无任何凭据字段
        r = client.get("/api/queue", headers=_auth(guest_tok))
        assert r.status_code == 200
        guest_item = r.json()[0]
        assert guest_item["episode"] == "S01E01"
        assert "share_code" not in guest_item
        for f in _SENSITIVE_QUEUE_FIELDS:
            assert f not in guest_item

        # admin 视图：share_code 仅后 4 位，其余凭据同样不返回
        r = client.get("/api/queue", headers=_auth(admin_tok))
        assert r.status_code == 200
        admin_item = r.json()[0]
        assert admin_item["share_code"] == "****XyZq"
        for f in _SENSITIVE_QUEUE_FIELDS:
            assert f not in admin_item

        # media 详情：episode_state 凭据分级（episode_state 按 updated_at/id 倒序，
        # 同更新时间时后插入的在先，故按 episode 索引断言）
        r = client.get(f"/api/media/{media_id}", headers=_auth(guest_tok))
        assert r.status_code == 200
        assert all(
            "share_code" not in e and "aria2_gid" not in e and "quark_path" not in e
            for e in r.json()["episode_state"]
        )
        r = client.get(f"/api/media/{media_id}", headers=_auth(admin_tok))
        by_ep = {e["episode"]: e for e in r.json()["episode_state"]}
        assert by_ep["S01E01"].get("share_code") == "****XyZq"
        assert by_ep["S01E02"].get("share_code") == "****Nm1p"

        # ---------- settings：权限 + 无凭据明文 ----------
        assert client.get("/api/settings", headers=_auth(guest_tok)).status_code == 403
        r = client.get("/api/settings", headers=_auth(admin_tok))
        assert r.status_code == 200
        data = r.json()
        assert "system_config" in data and "services" in data
        assert data["services"]["jwt_secret"] is True  # 测试设置了 JWT_SECRET
        assert all(isinstance(v, bool) for v in data["services"].values())  # 只回布尔

        # PATCH：非白名单键 → 422；白名单 UPSERT → 200
        assert (
            client.patch("/api/settings", json={"hack_key": "hack"}, headers=_auth(admin_tok)).status_code
            == 422
        )
        r = client.patch(
            "/api/settings",
            json={"quark_quota_gb": "210", "scheduler_enabled": "false"},
            headers=_auth(admin_tok),
        )
        assert r.status_code == 200, r.text
        r = client.get("/api/settings", headers=_auth(admin_tok))
        assert r.json()["system_config"].get("quark_quota_gb") == "210"
        # 还原配额配置，避免残留污染后续共享库的容量/告警测试
        # （全量跑时 capacity 测试读同一 SQLite 的 system_config，残留 210 会在
        # CI 等无 .env（默认配额 10）环境导致断言失败）
        client.patch("/api/settings", json={"quark_quota_gb": "10"}, headers=_auth(admin_tok))

        # Phase 8 配置入库：服务凭据可经 PATCH 写入 DB 且保存即生效；
        # GET 对敏感键不回显值（"***" 占位），services 布尔判定读 DB 值（非仅 env）。
        r = client.patch(
            "/api/settings",
            json={"tmdb_api_key": "db-tmdb-key"},
            headers=_auth(admin_tok),
        )
        assert r.status_code == 200, r.text
        r = client.get("/api/settings", headers=_auth(admin_tok))
        cfg = r.json()["system_config"]
        assert cfg.get("tmdb_api_key") == "***"  # 敏感键不回显明文
        assert r.json()["services"]["tmdb"] is True  # DB 来源凭据判定已配置
        assert "tmdb_api_key" in r.json().get("editable_keys", [])

        # ---------- logs：仅 admin ----------
        assert client.get("/api/logs", headers=_auth(guest_tok)).status_code == 403
        assert client.get("/api/logs", headers=_auth(admin_tok)).status_code == 200

        # ---------- tmdb.search：登录可用；无 key 时 503 可接受；未登录 401 ----------
        r = client.get("/api/tmdb/search", params={"q": "测试"}, headers=_auth(guest_tok))
        assert r.status_code in (200, 503)
        assert client.get("/api/tmdb/search", params={"q": "x"}).status_code == 401

        # ---------- 审批流：guest 提交 → admin 批准 → media 新增 ----------
        r = client.post(
            "/api/approvals",
            json={"title": "测试电影", "tmdb_id": 12345, "media_type": "movie", "poster_path": "/abc.jpg"},
            headers=_auth(guest_tok),
        )
        assert r.status_code == 200, r.text
        wr_id = r.json()["id"]

        # guest 列表只能看到自己的
        r = client.get("/api/approvals", headers=_auth(guest_tok))
        assert r.status_code == 200 and r.json()[0]["id"] == wr_id
        # admin 列表看到全部
        r = client.get("/api/approvals", headers=_auth(admin_tok))
        assert r.status_code == 200 and any(x["id"] == wr_id for x in r.json())

        # guest 无权 approve → 403
        assert (
            client.post(f"/api/approvals/{wr_id}/approve", headers=_auth(guest_tok)).status_code == 403
        )

        r = client.post(f"/api/approvals/{wr_id}/approve", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        new_media_id = r.json()["media_id"]

        # 重复 approve → 409
        assert (
            client.post(f"/api/approvals/{wr_id}/approve", headers=_auth(admin_tok)).status_code == 409
        )

        # media 列表出现新影视
        r = client.get("/api/media", headers=_auth(guest_tok))
        assert r.status_code == 200
        assert any(m["id"] == new_media_id and m["status"] == "tracking" for m in r.json())

        # ---------- media 写操作鉴权 ----------
        assert (
            client.patch(f"/api/media/{new_media_id}", json={"status": "paused"}, headers=_auth(guest_tok)).status_code
            == 403
        )
        r = client.patch(f"/api/media/{new_media_id}", json={"status": "paused"}, headers=_auth(admin_tok))
        assert r.status_code == 200 and r.json()["status"] == "paused"
        # 非法 status → 422
        assert (
            client.patch(f"/api/media/{new_media_id}", json={"status": "hacked"}, headers=_auth(admin_tok)).status_code
            == 422
        )

        # scan：guest 403 / admin 200
        assert (
            client.post(f"/api/media/{new_media_id}/scan", headers=_auth(guest_tok)).status_code == 403
        )
        r = client.post(f"/api/media/{new_media_id}/scan", headers=_auth(admin_tok))
        assert r.status_code == 200

        # ---------- queue retry：guest 403 / admin 200（failed→pending） ----------
        assert (
            client.post(f"/api/queue/{failed_tq_id}/retry", headers=_auth(guest_tok)).status_code == 403
        )
        r = client.post(f"/api/queue/{failed_tq_id}/retry", headers=_auth(admin_tok))
        assert r.status_code == 200, r.text
        # 再次 retry（已非 failed）→ 404
        assert (
            client.post(f"/api/queue/{failed_tq_id}/retry", headers=_auth(admin_tok)).status_code == 404
        )
        # episode_state 双表联动回 queued + retry_count 归零
        r = client.get(f"/api/media/{media_id}", headers=_auth(admin_tok))
        ep = [e for e in r.json()["episode_state"] if e["episode"] == "S01E02"][0]
        assert ep["state"] == "queued" and ep["retry_count"] == 0

        # ---------- 通知铃铛 ----------
        r = client.get("/api/notifications", headers=_auth(guest_tok))
        assert r.status_code == 200
        body = r.json()
        assert "items" in body and "unread_count" in body
        assert body["unread_count"] >= 1
        nid = body["items"][0]["id"]
        assert client.post(f"/api/notifications/{nid}/read", headers=_auth(guest_tok)).status_code == 200
        assert client.post("/api/notifications/read-all", headers=_auth(guest_tok)).status_code == 200

        # ---------- 邀请码删除：已用 409 / 未用 200 ----------
        assert client.delete(f"/api/admin/invites/{codes[0]}", headers=_auth(admin_tok)).status_code == 409
        assert client.delete(f"/api/admin/invites/{codes[1]}", headers=_auth(admin_tok)).status_code == 200
