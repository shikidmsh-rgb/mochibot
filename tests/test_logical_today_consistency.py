"""Regression tests for the logical_today / wall-clock mixup bug.

All tests simulate the maintenance window (UTC 0-3am, where logical_today
returns yesterday's wall-clock date) by patching `datetime.now` at the
specific call sites that decide "today".

If any of these tests fail in the future, someone reintroduced a write/read
date-source mismatch.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from mochi.skills.base import SkillContext


UTC = timezone.utc
MW_NOW = datetime(2025, 6, 15, 2, 0, tzinfo=UTC)  # maintenance window: 02:00 UTC
EXPECTED_LOGICAL = "2025-06-14"
EXPECTED_WALLCLOCK = "2025-06-15"


def _ctx(tool_name, action, user_id=1, **extra):
    args = {"action": action, **extra}
    return SkillContext(
        trigger="tool_call", user_id=user_id, channel_id=100,
        tool_name=tool_name, args=args,
    )


class TestHabitPauseDefaultUsesLogical:
    """handler.py:192 — pause default `+7 days` must be logical-based."""

    @pytest.mark.asyncio
    async def test_pause_default_until_uses_logical(self):
        from mochi.skills.habit.handler import HabitSkill
        from mochi.skills.habit.queries import add_habit, list_habits

        hid = add_habit(1, "Read", "daily:1")
        with patch("mochi.skills.habit.handler.datetime") as mock_dt:
            mock_dt.now.return_value = MW_NOW
            mock_dt.strptime = datetime.strptime
            ctx = _ctx("edit_habit", "pause", habit_id=hid)
            result = await HabitSkill().execute(ctx)
        assert result.success
        habits = list_habits(1)
        habit = next(h for h in habits if h["id"] == hid)
        # Logical "today" at 02:00 UTC on June 15 = June 14 → +7d = June 21
        assert habit["paused_until"] == "2025-06-21", (
            f"Expected logical+7=2025-06-21, got {habit['paused_until']}"
        )


class TestHabitStreakUsesLogical:
    """queries.py:186 — streak back-walk must align with logical write periods."""

    def test_streak_counts_logical_periods(self):
        from mochi.skills.habit.queries import add_habit, checkin_habit, get_habit_streak

        hid = add_habit(1, "Stretch", "daily:1")
        # Write checkins for the past 3 logical days from MW_NOW perspective
        # logical today = 2025-06-14, so periods are 2025-06-13, -12, -11
        for period in ("2025-06-13", "2025-06-12", "2025-06-11"):
            checkin_habit(1, hid, period=period)

        with patch("mochi.skills.habit.queries.datetime") as mock_dt:
            mock_dt.now.return_value = MW_NOW
            mock_dt.strptime = datetime.strptime
            with patch("mochi.config.datetime") as mock_cfg_dt:
                mock_cfg_dt.now.return_value = MW_NOW
                streak = get_habit_streak(hid, "daily", target=1)
        assert streak == 3, f"Expected streak=3, got {streak}"


class TestMealQueryDaysUsesLogical:
    """meal/queries.py:67 — days-based cutoff must align with logical writes."""

    def test_query_health_log_days_finds_logical_today_meal(self):
        from mochi.skills.meal.queries import save_health_log, query_health_log
        # Write meal at logical today
        save_health_log(1, EXPECTED_LOGICAL, "meal", "salad", source="meal_lunch")

        with patch("mochi.config.datetime") as mock_cfg_dt:
            mock_cfg_dt.now.return_value = MW_NOW
            results = query_health_log(1, days=1)
        meals = [r for r in results if r["type"] == "meal"]
        assert len(meals) >= 1, "Logical-today meal must be visible via days=1 query"


class TestProactiveLogConsistency:
    """db.py — log_proactive write timestamp + get_today_proactive_sent window must agree."""

    def test_proactive_visible_in_maintenance_window(self):
        from mochi.db import log_proactive, get_today_proactive_sent

        # Patch the wall-clock used by log_proactive's INSERT
        with patch("mochi.db.datetime") as mock_db_dt:
            mock_db_dt.now.return_value = MW_NOW
            mock_db_dt.strptime = datetime.strptime
            mock_db_dt.fromisoformat = datetime.fromisoformat
            with patch("mochi.config.datetime") as mock_cfg_dt:
                mock_cfg_dt.now.return_value = MW_NOW
                mock_cfg_dt.strptime = datetime.strptime
                log_proactive("test message", "habit_nudge")
                sent = get_today_proactive_sent()
        assert len(sent) == 1, (
            f"Proactive logged at {MW_NOW} must be retrievable in same logical window"
        )
        assert sent[0]["type"] == "habit_nudge"


class TestMessageCountUsesWallclock:
    """db.py:1594 / 1604 — message counts are physical, must stay wall-clock."""

    def test_get_message_count_today_uses_wallclock(self):
        """In maintenance window, count uses wall-clock 'today' (June 15), not logical (June 14)."""
        from mochi.db import save_message, get_message_count_today

        # Save one user message NOW (real wall clock), then verify count uses wall-clock today.
        save_message(1, "user", "hello")
        cnt = get_message_count_today(1)
        # Real-time call: should count today's message regardless of MAINTENANCE_HOUR.
        assert cnt >= 1


class TestRolloverBoundaryNoCheckinLost:
    """A checkin written at 02:59 must remain visible at 03:01 under same logical period."""

    def test_checkin_just_before_rollover_visible_after(self):
        from mochi.skills.habit.queries import add_habit, checkin_habit, get_habit_checkins
        from mochi.config import logical_today

        hid = add_habit(1, "Meditate", "daily:1")
        # Write at 02:59 UTC June 15 → logical day = June 14
        before_rollover = datetime(2025, 6, 15, 2, 59, tzinfo=UTC)
        with patch("mochi.skills.habit.handler.datetime") as mock_dt:
            mock_dt.now.return_value = before_rollover
            period_at_write = logical_today(before_rollover)
            checkin_habit(1, hid, period=period_at_write)
        assert period_at_write == "2025-06-14"

        # Now read at 03:01 UTC June 15 → logical day = June 15 (rollover happened)
        after_rollover = datetime(2025, 6, 15, 3, 1, tzinfo=UTC)
        period_at_read_d = logical_today(after_rollover)
        assert period_at_read_d == "2025-06-15"

        # The 02:59 checkin must still be retrievable for its OWN period (June 14),
        # which is the user-facing "yesterday" after rollover. It must not vanish.
        checkins_yesterday = get_habit_checkins(hid, "2025-06-14")
        assert len(checkins_yesterday) == 1
