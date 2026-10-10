from datetime import date

from app.utils import episode_aired

TODAY = date(2026, 10, 10)


def test_none_and_empty_not_aired():
    assert episode_aired(None, TODAY) is False
    assert episode_aired("", TODAY) is False


def test_past_date_aired():
    assert episode_aired("2026-10-09", TODAY) is True


def test_today_aired():
    assert episode_aired("2026-10-10", TODAY) is True


def test_future_not_aired():
    assert episode_aired("2026-10-11", TODAY) is False


def test_invalid_not_aired():
    assert episode_aired("not-a-date", TODAY) is False


def test_emby_datetime_string_supported():
    # Emby PremiereDate 为 ISO 8601 日期时间串
    assert episode_aired("2026-10-09T12:00:00.0000000Z", TODAY) is True
    assert episode_aired("2026-10-11T12:00:00.0000000Z", TODAY) is False
