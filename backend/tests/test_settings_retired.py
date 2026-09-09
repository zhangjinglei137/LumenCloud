"""废弃设置键不透传单测（remove-deprecated-settings）。

GET /api/settings 返回 system_config 全量行；已废弃键（download_queue_max_
concurrent）DB 存量保留，但响应层排除，避免前端 fallback 展示英文键名。
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lumencloud_settings_retired_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP
# Phase 8：JWT 密钥自动文件化；env 值仅用于 settings 面板 jwt_secret 布尔断言
os.environ["JWT_SECRET"] = "retired-secret-12345"
# 隔离外部服务：显式置空，避免真实网络调用（与 test_api_smoke.py 同模式）
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_PROXY"] = ""
os.environ["EMBY_BASE_URL"] = ""
os.environ["EMBY_API_KEY"] = ""
os.environ["ALIST_BASE_URL"] = ""
os.environ["ALIST_TOKEN"] = ""
os.environ["ARIA2_RPC_URL"] = ""
os.environ["ARIA2_TOKEN"] = ""
os.environ["NASTOOLS_BASE_URL"] = ""
os.environ["PUSHPLUS_TOKEN"] = ""
os.environ["CLOUDSAVER_BASE_URL"] = ""
os.environ["CLOUDSAVER_USERNAME"] = ""
os.environ["CLOUDSAVER_PASSWORD"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


async def _seed_retired_key():
    """注入存量废弃键（同事件循环），模拟历史 system_config 数据。"""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.database import async_session
    from app.models import SystemConfig

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session() as session:
        await session.merge(SystemConfig(key="download_queue_max_concurrent", value="50", updated_at=now))
        await session.commit()
        result = await session.execute(
            select(SystemConfig.value).where(SystemConfig.key == "download_queue_max_concurrent")
        )
        return result.scalars().first()


async def _recreate_admin() -> str:
    """删除 admin 后重新执行 ensure_admin，确定性拿到随机初始密码（Phase 8）。"""
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


def test_settings_response_excludes_retired_key():
    with TestClient(app) as client:
        # 存量废弃键注入成功（模拟 DB 中确有历史数据）
        assert client.portal.call(_seed_retired_key) == "50"

        # admin 登录
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        admin_tok = r.json()["access_token"]

        r = client.get("/api/settings", headers={"Authorization": f"Bearer {admin_tok}"})
        assert r.status_code == 200
        data = r.json()
        # 废弃键既不出现 system_config 也不出现 config（前端契约键）
        assert "download_queue_max_concurrent" not in data["system_config"]
        assert "download_queue_max_concurrent" not in data["config"]


def test_scan_interval_minutes_retired_and_not_editable():
    """scan_interval_minutes 从可编辑白名单移除，且存量数据 GET 不透传（remove-deprecated-settings）。"""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.database import async_session
    from app.models import SystemConfig

    # 注入存量数据（同事件循环，模拟历史 system_config 数据）
    async def _seed_scan_interval() -> str:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with async_session() as session:
            await session.merge(SystemConfig(key="scan_interval_minutes", value="60", updated_at=now))
            await session.commit()
            # 回查 DB 返回实际持久化值（同 _seed_retired_key 模式），避免「seeded」常量导致不透传断言空洞
            result = await session.execute(
                select(SystemConfig.value).where(SystemConfig.key == "scan_interval_minutes")
            )
            return result.scalars().first()

    with TestClient(app) as client:
        # 存量键注入成功且真实落库（断言实际持久化值），确保不透传断言基于真实存量数据
        assert client.portal.call(_seed_scan_interval) == "60"

        # admin 登录
        admin_password = client.portal.call(_recreate_admin)
        r = client.post("/api/auth/login", json={"username": "admin", "password": admin_password})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        res = client.get("/api/settings", headers=headers)
        assert res.status_code == 200
        body = res.json()
        # editable_keys 不含该键
        assert "scan_interval_minutes" not in body["editable_keys"]
        # GET 不透传存量值（system_config 与 config 均不含）
        assert "scan_interval_minutes" not in body["system_config"]
        assert "scan_interval_minutes" not in body["config"]

        # PATCH 该键 → 422
        patch = client.patch("/api/settings", headers=headers, json={"scan_interval_minutes": "60"})
        assert patch.status_code == 422