"""P6 aria2 hook 回调端点单测（POST /internal/aria2/notify）。

验证（对应 docs/影视下载两队列重设计.md §6.2 校验顺序）：
- 签名正确 → 200 {"ok": true}，且 trigger_download_complete(gid) 被调用
- 签名错误 → 401；签名头缺失 → 401
- 时间戳过期（±15min 外）→ 401（即使签名正确）
- secret 未配置 → 503（fail-closed）
- trigger_download_complete 未就绪（P5 未部署）→ 503「回调处理未就绪」
- gid 为空 → 400

测试方式：仅挂载 notify.router 的最小 FastAPI app（无 lifespan / 无 DB / 无
scheduler），用 TestClient 走真实 HTTP 层（header 读取、body 原文签名、HTTPException
→ 响应转换）。依赖注入：monkeypatch notify 模块内的 config_store（SimpleNamespace
fake，对齐 test_transfer.py 的 fake 风格）+ app.tasks.transfer 模块内的
trigger_download_complete（AsyncMock）。
"""
import hashlib
import hmac
import json
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.notify as notify_mod
import app.tasks.transfer as transfer_mod

_SECRET = "test-webhook-secret-0123456789abcdef"
_ENDPOINT = "/internal/aria2/notify"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sign(body_bytes: bytes, secret: str = _SECRET) -> str:
    """对 body 原文计算 HMAC-SHA256 签名（hex，与后端校验逻辑一致）。"""
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def make_body(gid: str = "gid-abc123", ts: int | None = None) -> bytes:
    """构造回调 body（ts 缺省为当前毫秒时间戳）。"""
    if ts is None:
        ts = int(time.time() * 1000)
    return json.dumps({"gid": gid, "ts": ts}).encode("utf-8")


def make_client(secret: str | None, monkeypatch) -> TestClient:
    """构建仅挂载 notify.router 的最小 app；config_store 用 fake 注入 secret。"""
    fake_store = types.SimpleNamespace(get=lambda key, default=None: secret)
    monkeypatch.setattr(notify_mod, "config_store", fake_store)
    app = FastAPI()
    app.include_router(notify_mod.router)
    return TestClient(app)


def mock_trigger(monkeypatch, return_value: bool = True) -> AsyncMock:
    """mock app.tasks.transfer.trigger_download_complete（P5 冻结签名）。

    raising=False：P5 未部署时模块尚无该属性，允许 monkeypatch 直接创建——
    测试结果与 P5 是否已实现无关；notify 的延迟 from-import 会取到 patch 后的 mock。"""
    mock = AsyncMock(return_value=return_value)
    monkeypatch.setattr(transfer_mod, "trigger_download_complete", mock, raising=False)
    return mock


# ---------------------------------------------------------------------------
# 签名正确 → 通过
# ---------------------------------------------------------------------------

def test_valid_signature_passes_and_calls_trigger(monkeypatch):
    """正确签名 + 新鲜时间戳 → 200 {"ok": true}，且 trigger 以 gid 被调用。

    P6 契约：推进成功与否（返回 bool）不区分应答——幂等由函数内部条件更新保证，
    回调受理即 ok。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch, return_value=True)

    body = make_body(gid="gid-good-1")
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    trigger.assert_awaited_once_with("gid-good-1")


def test_valid_signature_ok_even_when_trigger_returns_false(monkeypatch):
    """trigger 返回 False（幂等/未命中推进）→ 端点仍 200（回调已受理，不伪装推进）。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch, return_value=False)

    body = make_body(gid="gid-noop")
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    trigger.assert_awaited_once_with("gid-noop")


# ---------------------------------------------------------------------------
# 签名错误 / 头缺失 → 401
# ---------------------------------------------------------------------------

def test_bad_signature_401(monkeypatch):
    """body 用错误密钥签名 → 401「签名无效」（secret 与时间戳均就绪）。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch)

    body = make_body()
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body, secret="wrong-secret")},
    )

    assert r.status_code == 401, r.text
    assert "签名无效" in r.json()["detail"]
    trigger.assert_not_awaited()


def test_missing_signature_header_401(monkeypatch):
    """签名头缺失 → 401（空串不匹配，fail-closed）。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch)

    r = client.post(_ENDPOINT, content=make_body(), headers={"Content-Type": "application/json"})

    assert r.status_code == 401, r.text
    trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# 时间戳过期 → 401（即使签名正确）
# ---------------------------------------------------------------------------

def test_stale_timestamp_401(monkeypatch):
    """ts 超 ±15min（16 分钟前）→ 401「签名/时间戳无效」，先于签名校验。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch)

    stale_ts = int(time.time() * 1000) - 16 * 60 * 1000
    body = make_body(ts=stale_ts)  # 签名对该 body 计算，签名本身正确
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 401, r.text
    assert "签名/时间戳无效" in r.json()["detail"]
    trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# secret 未配置 → 503
# ---------------------------------------------------------------------------

def test_missing_secret_503(monkeypatch):
    """config_store 与 env 均无密钥 → 503（fail-closed：拒绝无鉴权回调）。"""
    client = make_client(None, monkeypatch)  # config_store.get 返回 None
    trigger = mock_trigger(monkeypatch)

    body = make_body()
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 503, r.text
    trigger.assert_not_awaited()


# ---------------------------------------------------------------------------
# trigger 未就绪（P5 未部署）→ 503
# ---------------------------------------------------------------------------

def test_trigger_not_ready_503(monkeypatch):
    """签名/时间戳通过但 trigger_download_complete 无法导入 → 503「回调处理未就绪」。

    用空模块替换 sys.modules 中的 app.tasks.transfer，强制 from-import 抛
    ImportError——测试结果与 P5 是否已部署无关（确定性覆盖未就绪分支）。"""
    client = make_client(_SECRET, monkeypatch)
    empty_module = types.ModuleType("app.tasks.transfer")
    monkeypatch.setitem(sys.modules, "app.tasks.transfer", empty_module)

    body = make_body(gid="gid-pending")
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 503, r.text
    assert "回调处理未就绪" in r.json()["detail"]


# ---------------------------------------------------------------------------
# gid 为空 → 400
# ---------------------------------------------------------------------------

def test_empty_gid_400(monkeypatch):
    """签名/时间戳通过但 gid 为空（缺省或空白）→ 400。"""
    client = make_client(_SECRET, monkeypatch)
    trigger = mock_trigger(monkeypatch)

    body = make_body(gid="")
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 400, r.text
    assert "gid 不能为空" in r.json()["detail"]
    trigger.assert_not_awaited()


def test_gid_whitespace_400(monkeypatch):
    """gid 为空白串 → 400（strip 后为空判空）。"""
    client = make_client(_SECRET, monkeypatch)

    body = make_body(gid="   ")
    r = client.post(
        _ENDPOINT,
        content=body,
        headers={"Content-Type": "application/json", "X-Aria2-Signature": sign(body)},
    )

    assert r.status_code == 400, r.text
    assert "gid 不能为空" in r.json()["detail"]
