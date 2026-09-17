"""Task B4 登出清除 httpOnly cookie 会话（Design D4）单测。

覆盖（对应 OpenSpec auth-session「登出清除服务端会话」requirement）：
- 登录后登出 → 响应 Set-Cookie 携带清除指令（access_token + Max-Age=0/过期）
- 登出后模拟浏览器残留旧 cookie 请求受保护端点 → 401（服务端令牌已被吊销，
  spec Scenario「登出后令牌不再有效」：使用残留 cookie 请求受保护端点返回 401）
- 未登录登出 → 200 幂等（不报错）
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.auth as auth_mod
from app.database import async_session
from app.models import User


async def _bootstrap_admin() -> str:
    """确保 admin 存在并重置其密码为确定性已知值（复用 test_auth_phase8 模式：
    不删行（防外键引用 IntegrityError），直接覆写 password_hash 拿可控密码登录）。
    经 TestClient portal 调度（portal.call 会 await 本协程）。"""
    password = "seed-pass-123"
    async with async_session() as session:
        user = await session.scalar(select(User).where(User.role == "admin"))
        if user is None:
            # 无 admin → ensure_admin 创建（幂等；随后统一覆写哈希）
            await auth_mod.ensure_admin()
            user = await session.scalar(select(User).where(User.role == "admin"))
        user.password_hash = auth_mod.hash_password(password)
        await session.commit()
    return password


def test_logout_clears_http_only_cookie():
    """登录后登出：响应 Set-Cookie 含 access_token 且 Max-Age=0（清除指令）。

    删除参数（key/path/httponly/samesite/secure）与登录 set_cookie 不一致时
    浏览器不会删除 cookie——断言响应确实携带了同名的过期指令。
    """
    from app.main import app

    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_admin)
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": password},
        ).json()["access_token"]
        assert token  # 登录成功拿到令牌

        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        set_cookies = r.headers.get_list("set-cookie")
        assert any(
            "access_token=" in c and "Max-Age=0" in c for c in set_cookies
        ), f"登出响应未携带 access_token 清除指令: {set_cookies}"


def test_stale_cookie_rejected_after_logout():
    """登出后残留旧 cookie 请求受保护端点 → 401（服务端令牌已吊销）。

    模拟浏览器未执行删除指令的「残留 cookie」场景：登出后手动把旧 token 值
    塞回 TestClient cookie jar（delete_cookie 已让 jar 移除，需显式构造残留）。
    当前实现（仅删 cookie、不吊销 token）下旧 token 依然有效 → 200，测试失败。
    """
    from app.main import app

    with TestClient(app) as client:
        password = client.portal.call(_bootstrap_admin)
        token = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": password},
        ).json()["access_token"]

        assert client.post("/api/auth/logout").status_code == 200
        # 残留 cookie：登出后 jar 已移除 access_token，手动塞回旧值模拟浏览器残留
        client.cookies.set("access_token", token)
        assert client.get("/api/auth/me").status_code == 401


def test_logout_without_login_is_idempotent():
    """未登录登出：恒 200（幂等，不报错）。"""
    from app.main import app

    with TestClient(app) as client:
        client.cookies.clear()
        r = client.post("/api/auth/logout")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
