"""docker-timezone：全局 UTC+Z 编码器单测。"""
from datetime import datetime, timezone

import pytest
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
