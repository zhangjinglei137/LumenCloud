"""Emby 遗漏集 aired-only 过滤单测（服务层，stub _get，不连真实 Emby）。

C-1（P1）：get_missing_episodes 请求带 Fields=PremiereDate（Emby 默认不返回该
字段，须显式请求），返回前剔除 PremiereDate 在未来（未播出）的集（预告/未来集
入漏判定，防追更误入队转存）；缺失/无法解析 PremiereDate 的集保守保留。
stub app.services.emby._get 整体绕过 _check_config/_base_url 与网络调用；同时
注入 config_store._cache（emby_base_url/emby_api_key）双保险，防意外路径走真配置。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import config_store as cs
from app.services import emby as emby_mod


def run(coro):
    return asyncio.run(coro)


_MISSING = object()  # 哨兵：PremiereDate 字段缺失
_PAST = "2024-05-12T03:00:00.0000000Z"      # 过去（7 位小数 + Z，仿 Emby 真实格式）
_FUTURE = "2099-01-01T00:00:00Z"            # 未来
_BAD = "bad-date"                            # 非标准字符串，无法解析


def _episode(num, premiere_date):
    """构造 Emby Missing Item；premiere_date=_MISSING 表示 PremiereDate 字段缺失。"""
    item = {
        "Id": f"id-{num}",
        "ParentIndexNumber": 1,
        "IndexNumber": num,
        "Name": f"EP{num}",
    }
    if premiere_date is not _MISSING:
        item["PremiereDate"] = premiere_date
    return item


@pytest.fixture()
def missing_get(monkeypatch):
    """stub emby._get（AsyncMock）+ config_store._cache（双保险，防意外路径）。"""
    mock = AsyncMock()
    monkeypatch.setattr(emby_mod, "_get", mock)
    monkeypatch.setattr(cs, "_cache", {
        "emby_base_url": "http://emby.test",
        "emby_api_key": "test-key",
    })
    return mock


def test_mixed_filters_future_only(missing_get):
    """过去 + 未来 + 缺失 + 非法混合 → 仅未来集被过滤，其余（过去/缺失/非法）保守保留。"""
    missing_get.return_value = {"Items": [
        _episode(1, _PAST),
        _episode(2, _FUTURE),
        _episode(3, _MISSING),
        _episode(4, _BAD),
    ]}

    result = run(emby_mod.get_missing_episodes("shows/1"))

    codes = {ep["code"] for ep in result}
    assert codes == {"S01E01", "S01E03", "S01E04"}  # 未来集 S01E02 被剔除

    # premiere_date 原样透传（Emby 原始串不转换），缺失保持 None
    by_code = {ep["code"]: ep for ep in result}
    assert by_code["S01E01"]["premiere_date"] == _PAST
    assert by_code["S01E03"]["premiere_date"] is None
    assert by_code["S01E04"]["premiere_date"] == _BAD


def test_all_future_returns_empty(missing_get):
    """全未来 → 返回空列表（含显式偏移 +00:00 与无时区 naive 两种表示）。"""
    missing_get.return_value = {"Items": [
        _episode(1, _FUTURE),
        _episode(2, "2099-06-01T00:00:00+00:00"),  # 无 Z、显式 UTC 偏移
        _episode(3, "2099-01-02T00:00:00"),        # naive 无时区 → 按 UTC 判定
    ]}

    assert run(emby_mod.get_missing_episodes("shows/1")) == []


def test_past_and_missing_all_kept(missing_get):
    """全过去/缺失 → 全部保留，计数与输入一致。"""
    items = [
        _episode(1, _PAST),
        _episode(2, _MISSING),
        _episode(3, "2024-01-01T12:00:00"),  # naive 过去时间
    ]
    missing_get.return_value = {"Items": items}

    result = run(emby_mod.get_missing_episodes("shows/1"))

    assert len(result) == len(items)
    assert {ep["code"] for ep in result} == {"S01E01", "S01E02", "S01E03"}


def test_request_requests_premiere_date_field(missing_get):
    """请求确实带 Fields=PremiereDate（Emby 默认不返回该字段，须显式请求）。"""
    missing_get.return_value = {"Items": [_episode(1, _PAST)]}

    run(emby_mod.get_missing_episodes("shows/1"))

    path, params = missing_get.await_args.args[:2]
    assert path == "/emby/Shows/Missing"
    assert params["ParentId"] == "shows/1"
    assert params["Fields"] == "PremiereDate"