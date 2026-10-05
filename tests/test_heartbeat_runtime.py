from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mochi import db, heartbeat, heartbeat_runtime as runtime


def test_free_time_plan_survives_restart_but_not_intervening_chat(monkeypatch):
    start = datetime(2026, 10, 4, 6, tzinfo=timezone.utc)
    options = dict(user_id=1, channel_id=1, transport="fake", now=start, max_daily=2, awake=True)
    draws = iter((0.0, 0.1, 0.0, 0.9))
    keys = runtime.ensure_daily_free_time_plan(**options, rng=SimpleNamespace(random=draws.__next__))
    assert len(keys) == 2
    assert runtime.ensure_daily_free_time_plan(**options) == []
    assert runtime.expire_abandoned_runs(now=start) == 0
    with db._connect() as conn:
        first_due = conn.execute(
            "SELECT next_attempt_at FROM heartbeat_runs ORDER BY next_attempt_at LIMIT 1",
        ).fetchone()[0]
    due = datetime.fromisoformat(first_due)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return due.astimezone(tz)

    monkeypatch.setattr(heartbeat, "datetime", Clock)
    generation = heartbeat.chat_activity_generation()
    assert heartbeat.free_time_turn_available(generation)
    with heartbeat.active_chat():
        assert not heartbeat.free_time_turn_available(generation)
    assert not heartbeat.has_active_chat()
    assert not heartbeat.free_time_turn_available(generation)
    with db._connect() as conn:
        rows = conn.execute("SELECT status,attempt_count FROM heartbeat_runs ORDER BY next_attempt_at").fetchall()
    assert sorted(tuple(row) for row in rows) == [("expired", 0), ("pending", 0)]
    heartbeat.go_to_sleep("explicit")
    heartbeat.reload_state_after_config_seed()
    assert heartbeat._state == heartbeat.SLEEPING
    assert not heartbeat.free_time_turn_available(None)
    assert runtime.get_schedulable_runs(now=due + timedelta(seconds=1)) == []
