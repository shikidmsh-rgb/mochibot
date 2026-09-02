"""Free Time window and daily quota planning."""

import random
from datetime import datetime, time, timedelta, timezone

import pytest

from mochi.db import _connect
from mochi.config import FREE_TIME_DAILY_MAX, FREE_TIME_MIN_GAP_MINUTES
from mochi.heartbeat_runtime import (
    FREE_TIME_AWAKE_END,
    FREE_TIME_AWAKE_START,
    ensure_daily_free_time_plan,
    free_time_clock_capacity,
    free_time_plan_bounds,
    in_free_time_window,
    max_free_time_slots,
    plan_free_time_slot_times,
)


UTC = timezone.utc


@pytest.fixture(autouse=True)
def _runtime_tz(monkeypatch):
    import mochi.heartbeat_runtime as runtime
    monkeypatch.setattr(runtime, "TZ", UTC)
    monkeypatch.setattr(
        runtime,
        "free_time_awake_clock",
        lambda: (runtime.FREE_TIME_AWAKE_START, runtime.FREE_TIME_AWAKE_END),
    )


def _pending_keys(prefix: str) -> list[str]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT run_key, next_attempt_at FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND status = 'pending' "
            "AND run_key LIKE ? ORDER BY next_attempt_at",
            (prefix,),
        ).fetchall()
        return [row["run_key"] for row in rows]
    finally:
        conn.close()


def test_plan_date_late_evening_stays_on_today():
    now = datetime(2026, 9, 1, 23, 30, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(now)
    assert plan_date == "2026-09-01"
    assert start == datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    assert in_free_time_window(now)


def test_plan_date_at_and_after_midnight_is_today_and_gap_until_8am():
    now = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(now)
    assert plan_date == "2026-09-02"
    assert start == datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    assert not in_free_time_window(now)
    later = datetime(2026, 9, 2, 1, 30, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(later)
    assert plan_date == "2026-09-02"
    assert not in_free_time_window(later)


def test_same_day_window_stays_on_today(monkeypatch):
    import mochi.heartbeat_runtime as runtime

    monkeypatch.setattr(
        runtime, "free_time_awake_clock", lambda: (time(9, 0), time(22, 0)),
    )
    now = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(now)
    assert plan_date == "2026-09-02"
    assert start == datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 22, 0, tzinfo=UTC)
    assert in_free_time_window(now)
    assert not in_free_time_window(datetime(2026, 9, 2, 23, 0, tzinfo=UTC))
    overnight = datetime(2026, 9, 3, 0, 15, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(overnight)
    assert plan_date == "2026-09-03"
    assert start == datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 3, 22, 0, tzinfo=UTC)
    assert not in_free_time_window(overnight)


def test_custom_wrap_window_keeps_hours_before_end_on_yesterday(monkeypatch):
    import mochi.heartbeat_runtime as runtime

    monkeypatch.setattr(
        runtime, "free_time_awake_clock", lambda: (time(22, 0), time(6, 0)),
    )
    now = datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(now)
    assert plan_date == "2026-09-01"
    assert start == datetime(2026, 9, 1, 22, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
    assert in_free_time_window(now)
    evening = datetime(2026, 9, 2, 22, 30, tzinfo=UTC)
    plan_date, start, end = free_time_plan_bounds(evening)
    assert plan_date == "2026-09-02"
    assert in_free_time_window(evening)


def test_default_clock_capacity_matches_hard_ceiling():
    assert free_time_clock_capacity(
        FREE_TIME_AWAKE_START, FREE_TIME_AWAKE_END,
    ) == FREE_TIME_DAILY_MAX


def test_timezone_change_rebuilds_plan_utc_instants(monkeypatch):
    import mochi.heartbeat_runtime as runtime
    from datetime import tzinfo

    class OffsetTZ(tzinfo):
        def __init__(self, hours: int):
            self._hours = hours

        def utcoffset(self, dt):
            return timedelta(hours=self._hours)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return f"UTC{self._hours:+d}"

    plus8 = OffsetTZ(8)
    monkeypatch.setattr(runtime, "TZ", plus8)
    ensure_daily_free_time_plan(
        user_id=1,
        channel_id=1,
        transport="telegram",
        now=datetime(2026, 9, 1, 8, 0, tzinfo=plus8),
        max_daily=4,
        rng=random.Random(0),
    )
    conn = _connect()
    try:
        before = [
            datetime.fromisoformat(row["next_attempt_at"]).astimezone(UTC)
            for row in conn.execute(
                "SELECT next_attempt_at FROM heartbeat_runs "
                "WHERE entry_kind = 'free_time' AND status = 'pending' "
                "ORDER BY next_attempt_at",
            )
        ]
        marker_before = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'",
        ).fetchone()["wake_reason"]
    finally:
        conn.close()
    assert "tz=8" in marker_before

    plus0 = OffsetTZ(0)
    monkeypatch.setattr(runtime, "TZ", plus0)
    ensure_daily_free_time_plan(
        user_id=1,
        channel_id=1,
        transport="telegram",
        now=datetime(2026, 9, 1, 8, 0, tzinfo=plus0),
        max_daily=4,
        rng=random.Random(0),
    )
    conn = _connect()
    try:
        after = [
            datetime.fromisoformat(row["next_attempt_at"]).astimezone(UTC)
            for row in conn.execute(
                "SELECT next_attempt_at FROM heartbeat_runs "
                "WHERE entry_kind = 'free_time' AND status = 'pending' "
                "ORDER BY next_attempt_at",
            )
        ]
        marker_after = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'",
        ).fetchone()["wake_reason"]
    finally:
        conn.close()
    assert "tz=0" in marker_after
    assert before != after


def test_schedules_at_least_configured_count_across_overnight_window():
    now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    created = ensure_daily_free_time_plan(
        user_id=1,
        channel_id=1,
        transport="telegram",
        now=now,
        max_daily=12,
        rng=random.Random(0),
    )
    assert len(created) == 12
    keys = _pending_keys("free_time:2026-09-01:%")
    assert len(keys) == 12
    conn = _connect()
    try:
        dues = [
            datetime.fromisoformat(row["next_attempt_at"])
            for row in conn.execute(
                "SELECT next_attempt_at FROM heartbeat_runs "
                "WHERE entry_kind = 'free_time' AND status = 'pending' "
                "ORDER BY next_attempt_at",
            )
        ]
    finally:
        conn.close()
    start = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    assert dues[0] >= start
    assert dues[-1] < end
    assert dues[-1] - dues[0] > timedelta(hours=8)
    gaps = [later - earlier for earlier, later in zip(dues, dues[1:])]
    assert gaps and min(gaps) >= timedelta(minutes=FREE_TIME_MIN_GAP_MINUTES)


def test_window_capacity_is_fifteen_minute_grid():
    start = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    assert max_free_time_slots(start, end) == FREE_TIME_DAILY_MAX
    assert FREE_TIME_DAILY_MAX == 64
    packed = plan_free_time_slot_times(start, end, 999, random.Random(0))
    assert len(packed) == 64
    assert packed[0] >= start
    assert packed[-1] < end
    gaps = [later - earlier for earlier, later in zip(packed, packed[1:])]
    assert min(gaps) >= timedelta(minutes=15)


def test_late_start_does_not_compress_into_the_leftover_window():
    now = datetime(2026, 9, 1, 16, 0, tzinfo=UTC)
    created = ensure_daily_free_time_plan(
        user_id=1,
        channel_id=1,
        transport="telegram",
        now=now,
        max_daily=10,
        rng=random.Random(0),
    )
    assert len(created) == 10
    pending = _pending_keys("free_time:2026-09-01:%")
    assert 0 < len(pending) < 10
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT run_key, status, next_attempt_at FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND run_key LIKE ?",
            ("free_time:2026-09-01:%",),
        ).fetchall()
    finally:
        conn.close()
    dues = sorted(
        datetime.fromisoformat(row["run_key"].split(":", 3)[3])
        for row in rows
    )
    start = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 9, 2, 0, 0, tzinfo=UTC)
    assert dues[0] >= start
    assert dues[0] < datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert dues[-1] < end
    assert dues[-1] - dues[0] > timedelta(hours=8)
    expired = [row for row in rows if row["status"] == "delivered"]
    assert len(expired) == 10 - len(pending)


def test_about_one_fifth_of_slots_get_direct_search():
    now = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    ensure_daily_free_time_plan(
        user_id=1,
        channel_id=1,
        transport="telegram",
        now=now,
        max_daily=10,
        rng=random.Random(0),
    )
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT facts_json FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND status = 'pending'",
        ).fetchall()
    finally:
        conn.close()
    import json
    flagged = sum(1 for row in rows if json.loads(row["facts_json"]).get("direct_search"))
    assert flagged == 2


def test_busy_and_sleep_cues_from_short_status():
    from mochi.heartbeat_runtime import unavailable_cue_from_text

    assert unavailable_cue_from_text("我在忙") == "busy"
    assert unavailable_cue_from_text("先忙，回头聊") == "busy"
    assert unavailable_cue_from_text("我睡了") == "sleep"
    assert unavailable_cue_from_text("晚安") == "sleep"
    assert unavailable_cue_from_text("睡不着") is None
    assert unavailable_cue_from_text("不忙") is None
    assert unavailable_cue_from_text("在忙" + "啊" * 80) is None


def test_skip_busy_within_floor_but_not_after():
    from mochi.heartbeat_runtime import should_skip_unavailable_slot

    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    recent = now - timedelta(minutes=10)
    stale = now - timedelta(minutes=50)
    assert should_skip_unavailable_slot(
        now=now, cue="busy", last_delivered_at=recent,
    ) == "busy"
    assert should_skip_unavailable_slot(
        now=now, cue="sleep", last_delivered_at=stale,
    ) is None
    assert should_skip_unavailable_slot(
        now=now, cue="busy", last_delivered_at=None,
    ) is None
    assert should_skip_unavailable_slot(
        now=now, cue=None, last_delivered_at=recent,
    ) is None


def test_sleeping_state_counts_as_unavailable():
    from mochi.heartbeat_runtime import owner_free_time_unavailable_cue

    assert owner_free_time_unavailable_cue(
        sleeping=True, last_user_text="刚回来",
    ) == "sleep"
    assert owner_free_time_unavailable_cue(
        sleeping=False, last_user_text="刚回来",
    ) is None
    assert owner_free_time_unavailable_cue(
        sleeping=False,
        last_user_text="刚回来",
        owner_spoken_since_wake=False,
    ) == "quiet_wake"


def test_quiet_wake_skips_until_floor_from_sleep_end():
    from mochi.heartbeat_runtime import should_skip_unavailable_slot

    wake = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)
    now = datetime(2026, 9, 2, 6, 20, tzinfo=UTC)
    assert should_skip_unavailable_slot(
        now=now,
        cue="quiet_wake",
        last_delivered_at=None,
        since=wake,
    ) == "quiet_wake"
    later = datetime(2026, 9, 2, 6, 50, tzinfo=UTC)
    assert should_skip_unavailable_slot(
        now=later,
        cue="quiet_wake",
        last_delivered_at=None,
        since=wake,
    ) is None
    last_night = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
    assert should_skip_unavailable_slot(
        now=now,
        cue="quiet_wake",
        last_delivered_at=last_night,
        since=wake,
    ) == "quiet_wake"


def test_quiet_wake_clears_after_owner_speaks():
    from mochi.heartbeat_runtime import owner_free_time_unavailable_cue

    assert owner_free_time_unavailable_cue(
        sleeping=False,
        last_user_text="早",
        owner_spoken_since_wake=True,
    ) is None


def _insert_run(*, run_key, status, next_attempt_at=None, text_delivered_at=None,
                outcome=None, attempt_count=0):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO heartbeat_runs "
            "(run_key, entry_kind, user_id, channel_id, transport, "
            "wake_reason, facts_json, status, next_attempt_at, "
            "text_delivered_at, outcome, attempt_count, created_at) "
            "VALUES (?, 'free_time', 1, 1, 'telegram', 'daily_random', "
            "'{}', ?, ?, ?, ?, ?, ?)",
            (
                run_key, status, next_attempt_at, text_delivered_at,
                outcome, attempt_count, "2026-09-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_skipped_slot_is_consumed_and_does_not_move_the_floor():
    from mochi.heartbeat_runtime import (
        claim_run,
        complete_without_delivery,
        last_delivered_free_time_at,
        should_skip_unavailable_slot,
    )
    from mochi.main_runtime import DurableChatResult

    delivered_at = datetime(2026, 9, 1, 11, 50, tzinfo=UTC)
    due = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    _insert_run(
        run_key="free_time:2026-09-01:0:done",
        status="delivered",
        text_delivered_at=delivered_at.isoformat(),
        outcome="delivered",
        attempt_count=1,
    )
    _insert_run(
        run_key="free_time:2026-09-01:1:due",
        status="pending",
        next_attempt_at=due.isoformat(),
    )
    last = last_delivered_free_time_at(1)
    assert should_skip_unavailable_slot(
        now=due, cue="busy", last_delivered_at=last,
    ) == "busy"
    claimed = claim_run("free_time:2026-09-01:1:due", now=due)
    assert claimed is not None
    assert complete_without_delivery(
        claimed, DurableChatResult(disposition="skip"), "skipped_busy",
    )
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT status, outcome, attempt_count, text_delivered_at "
            "FROM heartbeat_runs WHERE run_key = ?",
            ("free_time:2026-09-01:1:due",),
        ).fetchone()
    finally:
        conn.close()
    assert dict(row)["status"] == "delivered"
    assert dict(row)["outcome"] == "skipped_busy"
    assert dict(row)["text_delivered_at"] is None
    assert last_delivered_free_time_at(1) == delivered_at
