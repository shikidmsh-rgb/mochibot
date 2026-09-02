"""Admin 相处偏好: sleep hours and Free Time window."""

import pytest

from mochi.admin.preferences import normalize_preference_updates


CURRENT = {
    "TIMEZONE_OFFSET_HOURS": 8.0,
    "MAX_DAILY_FREE_TIME": 32,
    "SLEEP_AFTER_HOUR": 1,
    "WAKE_EARLIEST_HOUR": 8,
    "FREE_TIME_AWAKE_START": "08:00",
    "FREE_TIME_AWAKE_END": "00:00",
}


def test_accepts_schedule_clocks_and_hours():
    out = normalize_preference_updates(
        {
            "SLEEP_AFTER_HOUR": 2,
            "WAKE_EARLIEST_HOUR": 7,
            "FREE_TIME_AWAKE_START": "09:00",
            "FREE_TIME_AWAKE_END": "22:00",
            "MAX_DAILY_FREE_TIME": 12,
            "TIMEZONE_OFFSET_HOURS": 8,
        },
        CURRENT,
    )
    assert out["SLEEP_AFTER_HOUR"] == "2"
    assert out["WAKE_EARLIEST_HOUR"] == "7"
    assert out["FREE_TIME_AWAKE_START"] == "09:00"
    assert out["FREE_TIME_AWAKE_END"] == "22:00"
    assert out["MAX_DAILY_FREE_TIME"] == "12"


def test_normalizes_hour_from_clock_string():
    out = normalize_preference_updates(
        {"SLEEP_AFTER_HOUR": "01:00", "WAKE_EARLIEST_HOUR": "06:00"},
        CURRENT,
    )
    assert out["SLEEP_AFTER_HOUR"] == "1"
    assert out["WAKE_EARLIEST_HOUR"] == "6"


def test_rejects_identical_free_time_bounds():
    with pytest.raises(ValueError, match="must differ"):
        normalize_preference_updates(
            {"FREE_TIME_AWAKE_START": "08:00", "FREE_TIME_AWAKE_END": "08:00"},
            CURRENT,
        )


def test_rejects_quota_above_window_capacity():
    with pytest.raises(ValueError, match="between 0 and"):
        normalize_preference_updates(
            {
                "FREE_TIME_AWAKE_START": "09:00",
                "FREE_TIME_AWAKE_END": "10:00",
                "MAX_DAILY_FREE_TIME": 20,
            },
            CURRENT,
        )


def test_rejects_identical_sleep_hours():
    with pytest.raises(ValueError, match="SLEEP_AFTER_HOUR"):
        normalize_preference_updates(
            {"SLEEP_AFTER_HOUR": 6, "WAKE_EARLIEST_HOUR": 6},
            CURRENT,
        )


def test_accepts_clock_with_seconds():
    out = normalize_preference_updates(
        {"FREE_TIME_AWAKE_START": "08:00:00", "FREE_TIME_AWAKE_END": "00:00:00"},
        CURRENT,
    )
    assert out["FREE_TIME_AWAKE_START"] == "08:00"
    assert out["FREE_TIME_AWAKE_END"] == "00:00"


def test_rejects_hour_clock_with_minutes():
    with pytest.raises(ValueError, match="whole hours"):
        normalize_preference_updates(
            {"SLEEP_AFTER_HOUR": "01:30"},
            CURRENT,
        )


def test_rejects_unknown_preference():
    with pytest.raises(ValueError, match="Unknown preference"):
        normalize_preference_updates({"NOT_A_KEY": 1}, CURRENT)
