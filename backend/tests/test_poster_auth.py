"""海报代理端点鉴权测试（未登录 401）。"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="lumencloud_poster_auth_")
os.environ["LUMENCLOUD_DATA_DIR"] = _TMP
os.environ["JWT_SECRET"] = "poster-auth-secret-12345"
os.environ["TMDB_API_KEY"] = ""
os.environ["TMDB_POSTER_PROXY"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_poster_requires_auth():
    with TestClient(app) as client:
        # 未登录 → 401（不返回图片）
        r = client.get("/api/poster", params={"p": "/t/p/w500/x.jpg"})
        assert r.status_code == 401
