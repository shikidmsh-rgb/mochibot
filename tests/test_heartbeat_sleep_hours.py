"""Sleep/wake hour wrapping across midnight."""

from datetime import datetime, timezone

import mochi.heartbeat as heartbeat
from mochi.heartbeat import (
    _fallback_wake_due,
    _hour_in_half_open,
    _is_awake_hour,
    _is_rest_hour,
)

UTC = timezone.utc


def test_hour_range_wraps_past_midnight():
    assert _hour_in_half_open(23, 6, 1)
    assert _hour_in_half_open(0, 6, 1)
    assert not _hour_in_half_open(1, 6, 1)
    assert not _hour_in_half_open(5, 6, 1)
    assert _hour_in_half_open(6, 6, 1)


def test_awake_is_0600_to_0100(monkeypatch):
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 6)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 1)
    assert _is_awake_hour(23)
    assert _is_awake_hour(0)
    assert not _is_awake_hour(1)
    assert not _is_awake_hour(5)
    assert _is_awake_hour(6)
    assert _is_rest_hour(3)


def test_fallback_wake_from_sleep_end(monkeypatch):
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 6)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 1)
    assert not _fallback_wake_due(5)
    assert _fallback_wake_due(6)
    assert _fallback_wake_due(10)
    assert _fallback_wake_due(23)
    assert _fallback_wake_due(0)
    assert not _fallback_wake_due(1)


def test_overnight_sleep_is_rest_until_wake(monkeypatch):
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 7)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 23)
    assert _is_rest_hour(23)
    assert _is_rest_hour(0)
    assert _is_rest_hour(6)
    assert _is_awake_hour(7)
    assert _is_awake_hour(22)
    assert not _is_awake_hour(23)


def test_overnight_auto_wake_is_sleep_end(monkeypatch):
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 7)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 23)
    assert not _fallback_wake_due(6)
    assert _fallback_wake_due(7)


def test_should_wake_uses_awake_hours_when_sleep_wraps(monkeypatch):
    class _Clock:
        value = datetime(2026, 9, 2, 23, 15)

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(heartbeat, "_state", heartbeat.SLEEPING)
    monkeypatch.setattr(heartbeat, "_wake_earliest_hour", lambda: 7)
    monkeypatch.setattr(heartbeat, "_sleep_after_hour", lambda: 23)
    monkeypatch.setattr(heartbeat, "datetime", _Clock)
    assert not heartbeat.should_wake_on_message()
    _Clock.value = datetime(2026, 9, 2, 0, 10)
    assert not heartbeat.should_wake_on_message()
    _Clock.value = datetime(2026, 9, 2, 7, 0)
    assert heartbeat.should_wake_on_message()
    _Clock.value = datetime(2026, 9, 2, 22, 0)
    assert heartbeat.should_wake_on_message()


def test_awake_period_start_wraps_like_sleep_window(monkeypatch):
    monkeypatch.setattr(heartbeat, "TZ", UTC)
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 6)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 1)
    morning = datetime(2026, 9, 2, 6, 15, tzinfo=UTC)
    assert heartbeat.awake_period_start(morning) == datetime(
        2026, 9, 2, 6, 0, tzinfo=UTC,
    )
    predawn = datetime(2026, 9, 2, 0, 15, tzinfo=UTC)
    assert heartbeat.awake_period_start(predawn) == datetime(
        2026, 9, 1, 6, 0, tzinfo=UTC,
    )


def test_rest_hour_message_does_not_count_as_owner_up(monkeypatch):
    monkeypatch.setattr(heartbeat, "TZ", UTC)
    monkeypatch.setattr("mochi.heartbeat._wake_earliest_hour", lambda: 6)
    monkeypatch.setattr("mochi.heartbeat._sleep_after_hour", lambda: 1)
    now = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)
    rest_msg = datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
    after_wake = datetime(2026, 9, 2, 6, 30, tzinfo=UTC)
    assert not heartbeat.owner_spoken_since_awake(now, rest_msg)
    assert heartbeat.owner_spoken_since_awake(now, after_wake)
    assert not heartbeat.owner_spoken_since_awake(now, None)
