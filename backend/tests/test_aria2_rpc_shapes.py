"""aria2 RPC 参数形态回归测试（下载完成状态轮询链路）。

背景：`tell_status` 曾把 gid 包进数组传成 `[[gid], keys]`，aria2.tellStatus 实际期望
平铺的 `[gid, keys]` → 服务端拒绝 → HTTP 400 → 下载完成状态永远轮询不到，任务卡在
downloading（每分钟重试，recovery 超时才回滚）——凡人修仙传闭环「最后一段」根因。
"""
import asyncio

from app.services.aria2 import Aria2Client


def test_tell_status_params_flat():
    """tellStatus 参数必须为平铺 [gid, keys]（gid 为字符串、keys 为数组）。"""
    calls: list[tuple] = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        return {}

    client = Aria2Client()
    client._rpc = fake_rpc  # 实例级覆盖，直达参数形态断言
    asyncio.run(client.tell_status("688f826197b01f2d"))

    assert calls, "tell_status 应发起 RPC 调用"
    method, params = calls[0]
    assert method == "aria2.tellStatus"
    assert len(params) == 2
    assert params[0] == "688f826197b01f2d"  # gid 平铺为字符串（不能是 [gid]）
    assert isinstance(params[1], list)  # keys 数组
    assert params[1]  # keys 非空


def test_add_uri_and_remove_params_shapes():
    """其余 RPC 方法参数形态回归（addUri / remove）。"""
    import asyncio as _asyncio

    calls: list[tuple] = []

    async def fake_rpc(method, params):
        calls.append((method, params))
        if method == "aria2.addUri":
            return "gid123"
        return None

    client = Aria2Client()
    client._rpc = fake_rpc

    gid = _asyncio.run(client.add_uri("http://example/x.mkv", out="x.mkv", comment="lumencloud:1:S01E01"))
    assert gid == "gid123"
    m_add, p_add = calls[0]
    assert m_add == "aria2.addUri"
    assert p_add[0] == ["http://example/x.mkv"]  # 首参是 URI 数组
    assert p_add[1]["out"] == "x.mkv"
    assert p_add[1]["comment"] == "lumencloud:1:S01E01"

    _asyncio.run(client.remove("gid123"))
    m_rm, p_rm = calls[1]
    assert m_rm == "aria2.remove"
    assert p_rm == ["gid123"]


# ---------------------------------------------------------------------------
# token 前缀归一化回归（下载队列卡驻修复）
# ---------------------------------------------------------------------------
# 背景：.env 的 ARIA2_TOKEN 值本身已带 `token:` 前缀（token:zhangxiaolei...），
# _rpc 无脑再拼一次 `f"token:{token}"` → 线上 wire 值变 `token:token:...`
# → aria2 RPC 拒绝 → HTTP 400 Unauthorized → 下载队列永远 pending。
# 规则：配置值已带 `token:` 前缀时原样透传（单前缀），否则补前缀；空值保持传 ""。
from app.services import config_store as _cs
from app.services import aria2 as aria2_mod


class _FakePostResponse:
    """最小 fake httpx 响应：_rpc 只读 status_code 与 json()。"""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def json(self):
        return {"jsonrpc": "2.0", "id": "lumencloud", "result": "ok"}


class _FakePostClient:
    """最小 fake httpx.AsyncClient：记录 post(json=...) 构造的请求体（含 wire token）。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        self.calls.append(json or {})
        return _FakePostResponse(200)


def _rpc_body(monkeypatch, aria2_token: str) -> dict:
    """注入 config_store（aria2_rpc_url/aria2_token）并捕获 _rpc 发出的 body。"""
    http_client = _FakePostClient()
    monkeypatch.setattr(aria2_mod.httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setattr(_cs, "_cache", {
        "aria2_rpc_url": "http://aria2.test:6800/jsonrpc",
        "aria2_token": aria2_token,
    })
    asyncio.run(aria2_mod.client._rpc("aria2.getGlobalStat", []))
    assert len(http_client.calls) == 1, "_rpc 应恰好发出一发 POST"
    return http_client.calls[0]


def test_rpc_token_not_prefixed_twice_when_already_prefixed(monkeypatch):
    """配置 token 已带 `token:` 前缀 → wire params[0] 保持单前缀（token:token: 回归）。"""
    body = _rpc_body(monkeypatch, "token:zhangxiaolei-raw")
    assert body["params"][0] == "token:zhangxiaolei-raw"  # 不再重复添加前缀


def test_rpc_token_prefixed_when_plain(monkeypatch):
    """配置 token 不带前缀 → wire params[0] 自动补 `token:` 前缀（原契约保持）。"""
    body = _rpc_body(monkeypatch, "plain-secret")
    assert body["params"][0] == "token:plain-secret"


def test_rpc_token_empty_stays_empty(monkeypatch):
    """token 未配置 → wire params[0] 恒为 ""（无需鉴权时保持原行为）。"""
    body = _rpc_body(monkeypatch, "")
    assert body["params"][0] == ""