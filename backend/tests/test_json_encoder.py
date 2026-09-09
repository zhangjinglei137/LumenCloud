"""docker-timezone：全局 UTC+Z 编码器单测。"""
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder

from app.json import jsonable_encoder_zulu


def test_naive_datetime_appends_z():
    dt = datetime(2026, 9, 9, 1, 15, 0)  # naive，按 UTC 解释
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_aware_utc_normalized_to_z():
    dt = datetime(2026, 9, 9, 1, 15, 0, tzinfo=timezone.utc)
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_aware_plus0800_normalized_to_z():
    tz = timezone.__new__(timezone, __import__("datetime").timedelta(hours=8))
    dt = datetime(2026, 9, 9, 9, 15, 0, tzinfo=tz)
    assert jsonable_encoder_zulu(dt) == "2026-09-09T01:15:00Z"


def test_non_datetime_delegates_to_original():
    assert jsonable_encoder_zulu("hello") == "hello"
    assert jsonable_encoder_zulu({"a": 1}) == {"a": 1}
    assert jsonable_encoder_zulu(None) is None


def test_nested_datetime_in_dict():
    payload = {"created_at": datetime(2026, 9, 9, 1, 15, 0), "name": "x"}
    out = jsonable_encoder(payload, custom_encoder={datetime: jsonable_encoder_zulu})
    assert out["created_at"] == "2026-09-09T01:15:00Z"


def test_e2e_http_response_datetime_has_z():
    """端到端：install_zulu_encoder 替换 fastapi.routing.jsonable_encoder 后，
    真实 HTTP 响应经 serialize_response 路径，dict 内嵌套 datetime（naive 与
    aware +08:00）均归一为 UTC+Z 结尾（覆盖 main.py 接线后整条链路，而非仅
    直调 encoders 单测——修复轮 I1：此前嵌套 datetime 走原函数默认 isoformat
    无 Z，且无任何测试覆盖该真实路径）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.json import install_zulu_encoder

    install_zulu_encoder()
    app = FastAPI()

    @app.get("/t")
    async def get_time():
        return {
            "created_at": datetime(2026, 9, 9, 1, 15, 0),
            "local_at": datetime(2026, 9, 9, 9, 15, 0, tzinfo=timezone(timedelta(hours=8))),
            "name": "x",
        }

    with TestClient(app) as client:
        resp = client.get("/t")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_at"] == "2026-09-09T01:15:00Z"
    assert body["created_at"].endswith("Z")
    assert body["local_at"] == "2026-09-09T01:15:00Z"
    assert body["local_at"].endswith("Z")
    assert body["name"] == "x"
