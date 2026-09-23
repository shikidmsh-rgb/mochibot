"""Daily Free Time scheduling and cancellation, using the existing fixture DB."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.db import _connect
from mochi.main_runtime import DurableChatResult
import mochi.heartbeat_runtime as runtime


UTC = timezone.utc
DAY = datetime(2026, 9, 23, 5, tzinfo=UTC)


class Draws:
    def __init__(self, values):
        self.values = iter(values)

    def random(self):
        return next(self.values)


def plan(*, max_daily, now=DAY, draws):
    return runtime.ensure_daily_free_time_plan(
        user_id=1, channel_id=1, transport="fake", now=now,
        max_daily=max_daily, rng=Draws(draws),
    )


def rows():
    conn = _connect()
    try:
        return {
            row["run_key"]: dict(row)
            for row in conn.execute("SELECT * FROM heartbeat_runs").fetchall()
        }
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def runtime_clock(monkeypatch):
    monkeypatch.setattr(runtime, "TZ", UTC)
    monkeypatch.setattr(runtime, "_utc_now", lambda: DAY)


@pytest.mark.parametrize("limit, expected", [(0, 0), (5, 5), (50, 10)])
def test_daily_plan_is_bounded_idempotent_and_survives_startup(limit, expected):
    keys = plan(max_daily=limit, draws=[0.0, 0.5] * 10)
    assert len(keys) == expected
    assert plan(max_daily=limit, draws=[]) == []
    assert runtime.expire_abandoned_runs(now=DAY) == 0
    assert len(rows()) == expected
    assert all(
        row["status"] == "pending"
        and DAY.replace(hour=6) <= datetime.fromisoformat(row["next_attempt_at"])
        < DAY.replace(hour=21)
        for row in rows().values()
    )


def test_activation_cutoff_and_missed_times_do_not_catch_up():
    keys = plan(
        max_daily=4, now=DAY.replace(hour=12),
        draws=[0.6, 0.59, 0.1, 0.0, 0.5, 0.0, 0.9],
    )
    assert len(keys) == 2
    due = datetime.fromisoformat(rows()[keys[0]]["next_attempt_at"])
    assert runtime.get_schedulable_runs(now=due - timedelta(seconds=1)) == []
    assert runtime.claim_run(keys[0], now=due - timedelta(seconds=1), max_daily=4) is None
    assert len(runtime.get_schedulable_runs(now=due + timedelta(seconds=44))) == 1
    assert runtime.expire_unusable_free_time_runs(
        now=due + timedelta(seconds=45), active_chat=False, awake=True,
    ) == 1
    assert runtime.get_schedulable_runs(now=due + timedelta(seconds=45)) == []
    assert rows()[keys[1]]["status"] == "pending"
    assert runtime.expire_unusable_free_time_runs(
        now=DAY + timedelta(days=1), active_chat=False, awake=True,
    ) == 1
    assert runtime.get_schedulable_runs(now=DAY + timedelta(days=1)) == []


def test_setting_changes_and_failed_claims_share_one_daily_budget():
    keys = plan(max_daily=3, draws=[0.0, 0.1, 0.0, 0.2, 0.0, 0.9])
    due = datetime.fromisoformat(rows()[keys[0]]["next_attempt_at"])
    claimed = runtime.claim_run(keys[0], now=due, max_daily=3)
    assert claimed["attempt_count"] == 1
    assert runtime.record_failure(claimed, "no confirmed delivery", outcome="delivery_unknown")
    assert rows()[keys[0]]["attempt_count"] == 1
    assert runtime.claim_run(keys[0], now=due, max_daily=3) is None

    replacement = plan(max_daily=2, now=due, draws=[0.0, 0.8])
    assert len(replacement) == 1
    assert rows()[keys[1]]["outcome"] == "plan_replaced"
    replacement_due = datetime.fromisoformat(rows()[replacement[0]]["next_attempt_at"])
    assert runtime.claim_run(replacement[0], now=replacement_due, max_daily=1) is None
    assert runtime.claim_run(replacement[0], now=replacement_due, max_daily=2)
    assert plan(max_daily=1, now=replacement_due, draws=[]) == []
    assert plan(max_daily=2, now=replacement_due, draws=[]) == []
    assert sum(row["attempt_count"] for row in rows().values()) == 2


def test_abandoned_claims_expire_without_expiring_future_pending_plan():
    keys = plan(max_daily=3, draws=[0.0, 0.1, 0.0, 0.2, 0.0, 0.9])
    due = datetime.fromisoformat(rows()[keys[0]]["next_attempt_at"])
    first = runtime.claim_run(keys[0], now=due, max_daily=3)
    result = DurableChatResult(text="prepared, not confirmed")
    assert runtime.store_prepared_result(first, result)
    assert runtime.begin_delivery(first, now=due)
    second_due = datetime.fromisoformat(rows()[keys[1]]["next_attempt_at"])
    assert runtime.claim_run(keys[1], now=second_due, max_daily=3)

    assert runtime.expire_abandoned_runs(now=DAY.replace(hour=12)) == 2
    saved = rows()
    assert saved[keys[0]]["outcome"] == "delivery_unknown"
    assert saved[keys[0]]["result_json"] == result.to_json()
    assert saved[keys[1]]["outcome"] == "expired"
    assert saved[keys[2]]["status"] == "pending"
    assert saved[keys[2]]["next_attempt_at"] is not None
    assert runtime.claim_run(keys[0], now=DAY.replace(hour=12), max_daily=3) is None


@pytest.mark.asyncio
async def test_chat_scopes_balance_without_waiting_for_polling_task_completion(monkeypatch):
    import mochi.heartbeat as heartbeat

    keys = plan(max_daily=2, draws=[0.0, 0.1, 0.0, 0.9])
    due = datetime.fromisoformat(rows()[keys[0]]["next_attempt_at"])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return due.astimezone(tz)

    monkeypatch.setattr(heartbeat, "datetime", Clock)
    monkeypatch.setattr(heartbeat, "_state", heartbeat.AWAKE)
    monkeypatch.setattr(heartbeat, "_silent_pause", False)
    monkeypatch.setattr(heartbeat, "_active_chat_tokens", set())
    monkeypatch.setattr(heartbeat, "_chat_activity_generation", 0)
    monkeypatch.setattr(heartbeat, "_effective", lambda _key: 1)
    generation = heartbeat.chat_activity_generation()
    stamp = heartbeat._state_changed_at.isoformat()
    assert heartbeat.free_time_turn_available(generation, stamp)
    with pytest.raises(RuntimeError):
        with heartbeat.active_chat():
            assert heartbeat.has_active_chat()
            with heartbeat.active_chat():
                assert heartbeat.chat_activity_generation() == generation + 2
            assert heartbeat.has_active_chat()
            assert not heartbeat.free_time_turn_available(generation, stamp)
            raise RuntimeError("message failed")
    assert not heartbeat.has_active_chat()
    assert rows()[keys[0]]["outcome"] == "active_chat"
    assert rows()[keys[1]]["status"] == "pending"
    assert not heartbeat.free_time_turn_available(generation, stamp)
    assert heartbeat.free_time_turn_available(heartbeat.chat_activity_generation(), stamp)


@pytest.mark.parametrize("active_chat, awake", [(True, True), (False, False)])
def test_blocked_due_opportunity_expires_instead_of_waiting(active_chat, awake):
    keys = plan(max_daily=2, draws=[0.0, 0.1, 0.0, 0.9])
    due = datetime.fromisoformat(rows()[keys[0]]["next_attempt_at"])
    assert runtime.expire_unusable_free_time_runs(
        now=due, active_chat=active_chat, awake=awake,
    ) == 1
    saved = rows()
    assert saved[keys[0]]["outcome"] == ("active_chat" if active_chat else "asleep")
    assert saved[keys[0]]["attempt_count"] == 0
    assert saved[keys[1]]["status"] == "pending"
    assert runtime.get_schedulable_runs(now=due + timedelta(seconds=30)) == []
