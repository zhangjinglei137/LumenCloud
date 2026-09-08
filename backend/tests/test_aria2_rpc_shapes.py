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