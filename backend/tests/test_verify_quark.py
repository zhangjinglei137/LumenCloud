"""「验证 folderId」诊断接口单测（alist.diagnose_quark_mount）。

Q1 根因：cloudSaver save 用 quark_default_folder（folderId）与 alist Quark 驱动
root_folder_id 不一致 → save 落盘夸克其他目录 → alist /quark/{file_name} 永不可见
→ _get_link_wait_visible 轮询超时 → 转存失败。本单测覆盖后端诊断接口的
root_folder_id ↔ quark_default_folder 一致性比对逻辑。

网络层全 mock：monkeypatch 替换 alist 模块 _post/_get（list_dir 内部走 _post，
故 _post 按 body["path"] 分发）；注入 config_store._cache 与 settings fallback，
保证配置确定性。无 DB 依赖，不需要建 SQLite。
"""
import asyncio
import json

import app.services.alist as alist_mod
from app.config import settings
from app.services import config_store


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers：配置注入 + 网络桩
# ---------------------------------------------------------------------------

def _inject_env(monkeypatch, *, base="http://alist.test", token="tk-abc", folder="abc123"):
    """固定 settings/env fallback（config_store 缺失键时回落此值，保证确定性）。"""
    monkeypatch.setattr(settings, "ALIST_BASE_URL", base)
    monkeypatch.setattr(settings, "ALIST_TOKEN", token)
    monkeypatch.setattr(settings, "QUARK_DEFAULT_FOLDER", folder)


def _inject_cache(monkeypatch, **items):
    """注入 config_store 进程内缓存（= system_config 的 DB 值源；缺失键走 env fallback）。"""
    monkeypatch.setattr(config_store, "_cache", dict(items))


def _fs_entries(names):
    return {"content": [{"name": n, "is_dir": False, "size": len(n)} for n in names]}


def _storages(entries):
    """storage/list 的 data 字典（content 内为挂载条目列表）。"""
    return {"content": list(entries)}


def _install_network(monkeypatch, *, fs_payload=None, storage_payload=None,
                     post_error=None, get_error=None, calls=None):
    """安装 _post/_get 桩：list_dir 内部走 _post，故 _post 按 body["path"] 分发。"""
    async def fake_post(path, body):
        if calls is not None:
            calls["post"] += 1
        if post_error is not None:
            raise post_error
        if path == "/api/fs/list":
            return fs_payload
        raise AssertionError(f"unexpected POST: {path}")

    async def fake_get(path):
        if calls is not None:
            calls["get"] += 1
        if get_error is not None:
            raise get_error
        if path == "/api/admin/storage/list":
            return storage_payload
        raise AssertionError(f"unexpected GET: {path}")

    monkeypatch.setattr(alist_mod, "_post", fake_post)
    monkeypatch.setattr(alist_mod, "_get", fake_get)


# ---------------------------------------------------------------------------
# 用例 1：全一致 —— quark_default_folder == Quark 驱动 root_folder_id
# ---------------------------------------------------------------------------

def test_match_when_consistent(monkeypatch):
    _inject_env(monkeypatch)
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="abc123",
    )
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv", "f2.mkv"]),
        storage_payload=_storages([{
            "mount_path": "/quark",
            "driver": "Quark",
            "addition": json.dumps({"root_folder_id": "abc123", "root_folder_status": "work"}),
        }]),
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["match"] is True
    assert result["fs_list_ok"] is True
    assert result["quark_files"] == ["f1.mkv", "f2.mkv"]
    assert result["quark_file_count"] == 2
    assert result["quark_mount_found"] is True
    assert result["quark_mount_path"] == "/quark"
    assert result["quark_driver"] == "Quark"
    assert result["root_folder_id"] == "abc123"
    assert result["root_folder_status"] == "work"
    assert result["configured_folder_id"] == "abc123"
    assert result["storage_total"] == 1


# ---------------------------------------------------------------------------
# 用例 2：不一致（Q1 根因）—— quark_default_folder 与 root_folder_id 不同
# ---------------------------------------------------------------------------

def test_mismatch_q1_root_cause(monkeypatch):
    _inject_env(monkeypatch, folder="9b85abcd")
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="9b85abcd",
    )
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv"]),
        storage_payload=_storages([{
            "mount_path": "/quark",
            "driver": "Quark",
            "addition": json.dumps({"root_folder_id": "60bcef00"}),
        }]),
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["match"] is False
    assert result["root_folder_id"] == "60bcef00"      # 原样返回，不归一
    assert result["configured_folder_id"] == "9b85abcd"  # 原样返回，不归一
    assert result["quark_mount_found"] is True


# ---------------------------------------------------------------------------
# 用例 3：quark_default_folder 未配置（DB 无该键 + env fallback 为空串）
# ---------------------------------------------------------------------------

def test_folder_id_not_configured(monkeypatch):
    _inject_env(monkeypatch, folder="")
    _inject_cache(monkeypatch, alist_base_url="http://alist.test", alist_token="tk-abc")
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv"]),
        storage_payload=_storages([{
            "mount_path": "/quark",
            "driver": "Quark",
            "addition": json.dumps({"root_folder_id": "abc123"}),
        }]),
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["configured_folder_id"] is None
    assert result["match"] is None  # 配置侧缺失，无法比对
    assert result["root_folder_id"] == "abc123"  # alist 侧探测不受影响
    assert result["quark_mount_found"] is True


# ---------------------------------------------------------------------------
# 用例 4：alist 未配置（base/token 均空）→ 不发起任何网络请求
# ---------------------------------------------------------------------------

def test_alist_not_configured_no_network(monkeypatch):
    _inject_env(monkeypatch, base="", token="")
    _inject_cache(monkeypatch)
    calls = {"post": 0, "get": 0}
    _install_network(monkeypatch, calls=calls)
    result = run(alist_mod.diagnose_quark_mount())
    assert result["alist_configured"] is False
    assert calls == {"post": 0, "get": 0}  # 断言 _get/_post 完全未被调用
    assert result["quark_mount_found"] is False
    assert result["fs_list_ok"] is False
    assert result["fs_error"] is None
    assert result["quark_files"] == []
    assert result["storages"] == []
    assert result["match"] is None


# ---------------------------------------------------------------------------
# 用例 5：无 Quark 挂载 —— storage 只有 115 / aliyundrive
# ---------------------------------------------------------------------------

def test_no_quark_mount(monkeypatch):
    _inject_env(monkeypatch)
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="abc123",
    )
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["x.mkv"]),
        storage_payload=_storages([
            {"mount_path": "/115", "driver": "115", "addition": "{}"},
            {"mount_path": "/ali", "driver": "aliyundrive", "addition": "{}"},
        ]),
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["quark_mount_found"] is False
    assert result["quark_mount_path"] is None
    assert result["root_folder_id"] is None
    assert result["storage_total"] == 2
    assert result["storages"] == [
        {"mount_path": "/115", "driver": "115"},
        {"mount_path": "/ali", "driver": "aliyundrive"},
    ]
    assert result["match"] is None


# ---------------------------------------------------------------------------
# 用例 6：storage/list 返回 data 为 list 形态（alist v4 兼容）
# ---------------------------------------------------------------------------

def test_storage_list_v4_list_data(monkeypatch):
    _inject_env(monkeypatch)
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="abc123",
    )
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv"]),
        storage_payload=[
            {"mount_path": "/quark", "driver": "Quark",
             "addition": json.dumps({"root_folder_id": "abc123"})},
            {"mount_path": "/115", "driver": "115", "addition": "{}"},
        ],
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["storage_total"] == 2
    assert result["storages"] == [
        {"mount_path": "/quark", "driver": "Quark"},
        {"mount_path": "/115", "driver": "115"},
    ]
    assert result["quark_mount_found"] is True
    assert result["root_folder_id"] == "abc123"
    assert result["match"] is True


# ---------------------------------------------------------------------------
# 用例 7：fs/list 抛 AlistUnavailable → fs_list_ok=False，但 storage 探测仍执行
# ---------------------------------------------------------------------------

def test_fs_error_storage_still_probed(monkeypatch):
    _inject_env(monkeypatch)
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="abc123",
    )
    calls = {"post": 0, "get": 0}
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv"]),
        storage_payload=_storages([{
            "mount_path": "/quark",
            "driver": "Quark",
            "addition": json.dumps({"root_folder_id": "abc123"}),
        }]),
        post_error=alist_mod.AlistUnavailable("AList 请求失败: Connection refused"),
        calls=calls,
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["fs_list_ok"] is False
    assert result["fs_error"]  # 非空
    assert calls["get"] == 1  # storage 探测仍执行（_get 被调用）
    assert result["quark_mount_found"] is True
    assert result["root_folder_id"] == "abc123"
    assert result["match"] is True  # match 仍可判定


# ---------------------------------------------------------------------------
# 用例 8：addition 非合法 JSON → root_folder_id 为 None，不抛异常
# ---------------------------------------------------------------------------

def test_invalid_addition_json(monkeypatch):
    _inject_env(monkeypatch)
    _inject_cache(
        monkeypatch,
        alist_base_url="http://alist.test",
        alist_token="tk-abc",
        quark_default_folder="abc123",
    )
    _install_network(
        monkeypatch,
        fs_payload=_fs_entries(["f1.mkv"]),
        storage_payload=_storages([{
            "mount_path": "/quark",
            "driver": "Quark",
            "addition": "not-json",
        }]),
    )
    result = run(alist_mod.diagnose_quark_mount())
    assert result["quark_mount_found"] is True  # 挂载识别不受 addition 解析失败影响
    assert result["root_folder_id"] is None
    assert result["root_folder_status"] is None
    assert result["match"] is None
