"""最小大小配置面（change media-min-size-and-emby-redownload，车道 A 任务 1.1–1.3）。

与既有「最大大小」同构：全局常量 + settings 白名单 + Media 覆盖列 + media DTO 透出。
"""
import importlib.util
from pathlib import Path

from app.config import settings
from app.models import DownloadQueue, Media
from app.routers.media import MediaPatch, _media_dto
from app.routers.settings import _EDITABLE_KEYS, _WHITELIST_EXACT, _is_allowed_key


def test_default_min_size_constants():
    assert settings.DEFAULT_MIN_EPISODE_SIZE_GB == 0.1
    assert settings.DEFAULT_MIN_MOVIE_SIZE_GB == 0.5


def test_min_size_keys_patchable():
    for key in ("min_episode_size_gb", "min_movie_size_gb"):
        assert key in _WHITELIST_EXACT
        assert _is_allowed_key(key)
        assert key in _EDITABLE_KEYS


def test_media_min_size_columns_nullable():
    for name in ("min_episode_size_gb", "min_movie_size_gb"):
        assert Media.__table__.c[name].nullable is True


def test_download_queue_auto_redownload_count_column():
    col = DownloadQueue.__table__.c["auto_redownload_count"]
    assert col.nullable is False
    assert str(col.server_default.arg) == "0"


def test_migration_0019_chain():
    path = (
        Path(__file__).resolve().parent.parent
        / "alembic/versions/0019_media_min_size_auto_redownload.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0019", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0019_media_min_size"
    assert len(mod.revision) <= 32
    assert mod.down_revision == "0018_user_token_version"


def test_media_patch_accepts_min_size():
    patch = MediaPatch(min_episode_size_gb=0.2, min_movie_size_gb=1.5)
    assert patch.model_dump(exclude_unset=True) == {
        "min_episode_size_gb": 0.2,
        "min_movie_size_gb": 1.5,
    }


def test_media_dto_echoes_min_size():
    m = Media(title="最小大小测试", min_episode_size_gb=0.2, min_movie_size_gb=1.5)
    dto = _media_dto(m)
    assert dto["min_episode_size_gb"] == 0.2
    assert dto["min_movie_size_gb"] == 1.5
