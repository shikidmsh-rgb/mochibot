"""Tests for the habit skill handler — add, checkin, undo, pause, resume, list, stats."""

import sqlite3

import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.habit.handler import HabitSkill
from mochi.skills.habit.queries import add_habit, checkin_habit, list_habits, get_habit_checkins


def _ctx(tool_name, action, user_id=1, **extra):
    """Helper to build a SkillContext for habit tests."""
    args = {"action": action, **extra}
    return SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=100,
        tool_name=tool_name,
        args=args,
    )


class TestEditHabitAdd:

    @pytest.mark.asyncio
    async def test_add_daily_habit(self):
        """Adding a daily:1 habit creates it successfully."""
        ctx = _ctx("edit_habit", "add", name="Drink water", frequency="daily:1")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "created" in result.output.lower() or "Drink water" in result.output
        habits = list_habits(1)
        assert len(habits) == 1
        assert habits[0]["name"] == "Drink water"

    @pytest.mark.asyncio
    async def test_add_weekly_habit(self):
        """Adding a weekly:3 habit works."""
        ctx = _ctx("edit_habit", "add", name="Exercise", frequency="weekly:3")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "Exercise" in result.output

    @pytest.mark.asyncio
    async def test_add_invalid_frequency(self):
        """Invalid frequency string is rejected."""
        ctx = _ctx("edit_habit", "add", name="Bad", frequency="invalid")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "invalid" in result.output.lower()

    @pytest.mark.asyncio
    async def test_add_missing_name(self):
        """Missing name returns error."""
        ctx = _ctx("edit_habit", "add", frequency="daily:1")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "name" in result.output.lower()

    @pytest.mark.asyncio
    async def test_add_missing_frequency(self):
        """Missing frequency returns error."""
        ctx = _ctx("edit_habit", "add", name="Test")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "frequency" in result.output.lower()

    @pytest.mark.asyncio
    async def test_add_duplicate_name(self):
        """Adding a habit with duplicate name returns error."""
        add_habit(1, "Walk", "daily:1")
        ctx = _ctx("edit_habit", "add", name="Walk", frequency="daily:2")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "already exists" in result.output.lower()

    @pytest.mark.asyncio
    async def test_add_with_importance(self):
        """Importance flag appears in output."""
        ctx = _ctx("edit_habit", "add", name="Study", frequency="daily:1", importance="important")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "important" in result.output.lower()


class TestCheckinHabit:

    @pytest.mark.asyncio
    async def test_checkin_increments(self):
        """Checkin on a daily:2 habit increments count."""
        hid = add_habit(1, "Read", "daily:2")
        ctx = _ctx("checkin_habit", "checkin", habit_id=hid)
        with patch("mochi.diary.refresh_diary_status"):
            result = await HabitSkill().execute(ctx)
        assert result.success
        assert "1/2" in result.output

    @pytest.mark.asyncio
    async def test_checkin_completes(self):
        """Checkin that hits the target shows completion."""
        hid = add_habit(1, "Run", "daily:1")
        ctx = _ctx("checkin_habit", "checkin", habit_id=hid)
        with patch("mochi.diary.refresh_diary_status"):
            result = await HabitSkill().execute(ctx)
        assert result.success
        assert "completed" in result.output.lower() or "1/1" in result.output

    @pytest.mark.asyncio
    async def test_checkin_already_done(self):
        """Checkin when target already met returns 'already completed'."""
        hid = add_habit(1, "Stretch", "daily:1")
        # Pre-fill one checkin via DB directly
        from mochi.config import logical_today
        period = logical_today()
        checkin_habit(hid, 1, period)
        ctx = _ctx("checkin_habit", "checkin", habit_id=hid)
        with patch("mochi.diary.refresh_diary_status"):
            result = await HabitSkill().execute(ctx)
        assert "already completed" in result.output.lower() or "🎉" in result.output

    @pytest.mark.asyncio
    async def test_checkin_not_found(self):
        """Checkin on nonexistent habit returns error."""
        ctx = _ctx("checkin_habit", "checkin", habit_id=9999)
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_checkin_multi_count(self):
        """Checkin with count=2 on daily:3 records 2 entries."""
        hid = add_habit(1, "Water", "daily:3")
        ctx = _ctx("checkin_habit", "checkin", habit_id=hid, count=2)
        with patch("mochi.diary.refresh_diary_status"):
            result = await HabitSkill().execute(ctx)
        assert result.success
        assert "2/3" in result.output


class TestUndoCheckin:

    @pytest.mark.asyncio
    async def test_undo_success(self):
        """Undo removes last checkin."""
        hid = add_habit(1, "Meditate", "daily:2")
        from mochi.config import logical_today
        period = logical_today()
        checkin_habit(hid, 1, period)
        ctx = _ctx("checkin_habit", "undo_checkin", habit_id=hid)
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "undone" in result.output.lower()
        assert "0/2" in result.output

    @pytest.mark.asyncio
    async def test_undo_no_checkins(self):
        """Undo with no checkins returns 'nothing to undo'."""
        hid = add_habit(1, "Yoga", "daily:1")
        ctx = _ctx("checkin_habit", "undo_checkin", habit_id=hid)
        result = await HabitSkill().execute(ctx)
        assert "nothing to undo" in result.output.lower() or "no checkins" in result.output.lower()


class TestPauseResume:

    @pytest.mark.asyncio
    async def test_pause_success(self):
        """Pause sets paused_until date."""
        hid = add_habit(1, "Journal", "daily:1")
        ctx = _ctx("edit_habit", "pause", habit_id=hid, until="2099-12-31")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "paused" in result.output.lower()
        assert "2099-12-31" in result.output

    @pytest.mark.asyncio
    async def test_resume_success(self):
        """Resume clears paused_until."""
        hid = add_habit(1, "Piano", "daily:1")
        from mochi.skills.habit.queries import pause_habit
        pause_habit(1, hid, "2099-12-31")
        ctx = _ctx("edit_habit", "resume", habit_id=hid)
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "resumed" in result.output.lower()

    @pytest.mark.asyncio
    async def test_pause_invalid_date(self):
        """Pause with bad date format returns error."""
        hid = add_habit(1, "Draw", "daily:1")
        ctx = _ctx("edit_habit", "pause", habit_id=hid, until="not-a-date")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "invalid" in result.output.lower()


class TestQueryHabit:

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """List habits when none exist returns 'No active habits'."""
        ctx = _ctx("query_habit", "list")
        result = await HabitSkill().execute(ctx)
        assert "no active habits" in result.output.lower()

    @pytest.mark.asyncio
    async def test_list_with_habits(self):
        """List shows existing habits."""
        add_habit(1, "Clean", "daily:1")
        add_habit(1, "Cook", "weekly:3")
        ctx = _ctx("query_habit", "list")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "Clean" in result.output
        assert "Cook" in result.output

    @pytest.mark.asyncio
    async def test_stats(self):
        """Stats returns habit information."""
        hid = add_habit(1, "Walk", "daily:1")
        ctx = _ctx("query_habit", "stats", habit_id=hid)
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "Walk" in result.output


class TestEditHabitOther:

    @pytest.mark.asyncio
    async def test_remove_habit(self):
        """Remove deactivates a habit."""
        hid = add_habit(1, "Trash", "daily:1")
        ctx = _ctx("edit_habit", "remove", habit_id=hid)
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "deactivated" in result.output.lower()

    @pytest.mark.asyncio
    async def test_update_habit(self):
        """Update changes habit fields."""
        hid = add_habit(1, "Old Name", "daily:1")
        ctx = _ctx("edit_habit", "update", habit_id=hid, name="New Name")
        result = await HabitSkill().execute(ctx)
        assert result.success
        assert "updated" in result.output.lower()

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        """Unknown action on edit_habit returns error."""
        ctx = _ctx("edit_habit", "fly")
        result = await HabitSkill().execute(ctx)
        assert not result.success
        assert "Unknown" in result.output
