"""A-2（P1）双层开关配置断层统一：job 级未配置跟随总开关（方案②）。

只测纯逻辑 get_job_enabled：monkeypatch app.scheduler._get_config_value 为返回
配置 dict 的异步 fake（不碰 DB、不启动 scheduler）。import app.scheduler 会创建
全局 scheduler 实例但不 start（register_jobs/start 为显式调用），无害。

背景：旧语义 job 级未配置回落 _JOB_DEFAULT_ENABLED 全 False → 打开总开关
（scheduler_enabled 默认 true）实际所有 job 仍不运行。方案②改为 job 级未配置
跟随总开关；显式 scheduler.<job_id>=true/false 仍尊重覆盖。
"""
import asyncio

import pytest

import app.scheduler as scheduler_mod
from app.scheduler import get_job_enabled


def run(coro):
    return asyncio.run(coro)


def _fake_get_config_value(config: dict):
    """异步 fake：键不存在返回 default（与真实 _get_config_value 语义一致）。"""

    async def _fake(key: str, default=None):
        return config.get(key, default)

    return _fake


def _enabled(cfg, monkeypatch, job_id=scheduler_mod.JOB_SCAN_ALL_MEDIA):
    monkeypatch.setattr(scheduler_mod, "_get_config_value", _fake_get_config_value(cfg))
    return run(get_job_enabled(job_id))


def test_default_global_on_job_follows(monkeypatch):
    """总开关未配置（默认 true）+ job 未配置 → True（A-2 核心断言：打开总开关即默认全开）。"""
    assert _enabled({}, monkeypatch) is True
    # 全部 7 个固定 job 未配置时均跟随总开关默认启用
    for jid in scheduler_mod.JOB_IDS:
        assert _enabled({}, monkeypatch, jid) is True


def test_global_true_job_explicit_false(monkeypatch):
    """总开关 true + job 显式 false → False（显式关闭仍生效）。"""
    cfg = {
        "scheduler_enabled": "true",
        f"scheduler.{scheduler_mod.JOB_SCAN_ALL_MEDIA}": "false",
    }
    assert _enabled(cfg, monkeypatch) is False


def test_global_false_job_explicit_true(monkeypatch):
    """总开关 false + job 显式 true → False（总开关短路，job 级无法越权）。"""
    cfg = {
        "scheduler_enabled": "false",
        f"scheduler.{scheduler_mod.JOB_SCAN_ALL_MEDIA}": "true",
    }
    assert _enabled(cfg, monkeypatch) is False


def test_global_true_job_explicit_true(monkeypatch):
    """总开关 true + job 显式 true → True。"""
    cfg = {
        "scheduler_enabled": "true",
        f"scheduler.{scheduler_mod.JOB_SCAN_ALL_MEDIA}": "true",
    }
    assert _enabled(cfg, monkeypatch) is True


def test_config_read_error_propagates(monkeypatch):
    """读取 _get_config_value 抛异常 → get_job_enabled 不吞异常、向上传播。

    get_job_enabled 无 try/except，异常交由 _apply_job_switches 的 try 捕获后
    降级为暂停（fail-closed：读不到配置=不启用定时）。
    """

    async def _boom(key: str, default=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(scheduler_mod, "_get_config_value", _boom)
    with pytest.raises(RuntimeError):
        run(get_job_enabled(scheduler_mod.JOB_SCAN_ALL_MEDIA))
