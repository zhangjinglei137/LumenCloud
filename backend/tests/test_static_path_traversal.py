"""安全审查 D7：SPA fallback 路径穿越防护（Critical）。

线上风险：main.py serve_spa 以 `STATIC_DIR / full_path` 直接拼接用户可控的
`full_path` 路径参数，未做规范化校验——URL 编码的 `..%2f` 可穿越出 static
根目录读取任意文件（如 <data_dir>/.jwt_secret 伪造任意身份 token、
lumencloud.db 等），属任意文件读取。

TDD：先写本测试（RED：修复前 `..%2f` 可读越界机密文件），再实现防护（GREEN）。
"""
import os
import tempfile

from fastapi.testclient import TestClient

_TMP_DATA = tempfile.mkdtemp(prefix="lumencloud_path_trav_")
os.environ.setdefault("LUMENCLOUD_DATA_DIR", _TMP_DATA)
# 隔离外部服务凭据（测试不发起外部调用）
for _K in (
    "TMDB_API_KEY", "TMDB_PROXY", "CLOUDSAVER_BASE_URL", "CLOUDSAVER_USERNAME",
    "CLOUDSAVER_PASSWORD", "EMBY_BASE_URL", "EMBY_API_KEY", "ALIST_BASE_URL",
    "ALIST_TOKEN", "ARIA2_RPC_URL", "ARIA2_TOKEN", "NASTOOLS_BASE_URL", "PUSHPLUS_TOKEN",
):
    os.environ[_K] = ""

import app.main as main_mod  # noqa: E402


def _make_static_layout(tmp_path) -> "os.PathLike":
    """构造 static 根（含 index.html / 正常静态文件）+ static 外的机密文件。"""
    from pathlib import Path

    static_dir = Path(tmp_path) / "backend" / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("SPA-INDEX", encoding="utf-8")
    (static_dir / "normal.txt").write_text("NORMAL-FILE", encoding="utf-8")
    # static 根之外的机密文件（模拟 <data_dir>/.jwt_secret 等越界目标）
    (Path(tmp_path) / "secret.txt").write_text("TOP-SECRET-FILE", encoding="utf-8")
    return static_dir


def test_spa_fallback_cannot_read_outside_static_root(monkeypatch, tmp_path):
    """URL 编码的 ..（..%2f 等）不可穿越出 static 根读取越界文件。

    RED 证据：修复前 `..%2f..%2fsecret.txt` 可读到 TOP-SECRET-FILE；
    GREEN：所有穿越载荷不得在响应中出现机密内容。
    """
    static_dir = _make_static_layout(tmp_path)
    monkeypatch.setattr(main_mod, "STATIC_DIR", static_dir)
    with TestClient(main_mod.app) as client:
        for payload in (
            "..%2fsecret.txt",            # 上一级（不存在 → 回退，不得 500）
            "..%2f..%2fsecret.txt",       # 穿越两级 → 命中机密文件（修复前可读）
            "%2e%2e%2fsecret.txt",        # 点号编码变体
            "..%2f..%2f..%2fetc%2fpasswd",  # 更深穿越
        ):
            r = client.get(f"/{payload}")
            assert "TOP-SECRET-FILE" not in r.text, payload
            assert r.status_code in (200, 404), payload


def test_spa_fallback_serves_normal_static_files(monkeypatch, tmp_path):
    """static 根内的正常静态文件仍可直出（回归锁定，不误伤静态资源）。"""
    static_dir = _make_static_layout(tmp_path)
    monkeypatch.setattr(main_mod, "STATIC_DIR", static_dir)
    with TestClient(main_mod.app) as client:
        r = client.get("/normal.txt")
        assert r.status_code == 200, r.text
        assert r.text == "NORMAL-FILE"


def test_spa_fallback_serves_index_for_unknown_path(monkeypatch, tmp_path):
    """未知路径保持 SPA fallback（index.html 渲染，语义不变）。"""
    static_dir = _make_static_layout(tmp_path)
    monkeypatch.setattr(main_mod, "STATIC_DIR", static_dir)
    with TestClient(main_mod.app) as client:
        r = client.get("/some/unknown/route")
        assert r.status_code == 200, r.text
        assert "SPA-INDEX" in r.text
