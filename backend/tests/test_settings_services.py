"""Q5（P2）settings.services.jwt_secret 判定单测：按密钥文件存在性判定。

背景：jwt_secret 不存 DB——app/config.py 的 load_or_create_jwt_secret 首启自动
生成 <data_dir>/.jwt_secret（chmod 600），重启读文件，永久有效。旧判定读
config_store.get("jwt_secret", settings.JWT_SECRET)，DB 无键时 fallback 默认值
"change_me" → 永远误报「jwt_secret 未配置」；修复后按密钥文件是否已落盘判定
（load_or_create 保证文件存在即有效随机密钥，空文件会被重写为有效值）。

_services_configured 是同步函数，直接调用断言即可；函数内 `s` 即
app.config.settings 实例（settings.py 顶部 `from app.config import settings as
app_settings`，函数内 `s = app_settings`），故 monkeypatch
app.config.settings.LUMENCLOUD_DATA_DIR 即可生效。
"""
from pathlib import Path

from app.config import settings
from app.routers.settings import _services_configured


def _point_data_dir(tmp_path, monkeypatch) -> Path:
    """把 settings.LUMENCLOUD_DATA_DIR 指向 pytest 临时目录并返回该目录。"""
    monkeypatch.setattr(settings, "LUMENCLOUD_DATA_DIR", str(tmp_path))
    return tmp_path


def test_jwt_secret_configured_when_file_exists(tmp_path, monkeypatch):
    """密钥文件已落盘（load_or_create 正常生成）→ 判定已配置。"""
    data_dir = _point_data_dir(tmp_path, monkeypatch)
    (data_dir / ".jwt_secret").write_text("abc123", encoding="utf-8")

    assert _services_configured()["jwt_secret"] is True


def test_jwt_secret_not_configured_without_file(tmp_path, monkeypatch):
    """密钥文件尚未生成（首启前/目录为空）→ 判定未配置。"""
    _point_data_dir(tmp_path, monkeypatch)

    assert _services_configured()["jwt_secret"] is False


def test_jwt_secret_configured_when_file_empty(tmp_path, monkeypatch):
    """文件存在但内容为空 → 仍判定已配置（load_or_create 会把空文件重写为有效值）。"""
    data_dir = _point_data_dir(tmp_path, monkeypatch)
    (data_dir / ".jwt_secret").write_text("", encoding="utf-8")

    assert _services_configured()["jwt_secret"] is True
