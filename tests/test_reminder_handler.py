"""Tests for mochi/skills/reminder/handler.py — ReminderSkill."""

import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.reminder.handler import ReminderSkill


def _make_ctx(action: str, user_id: int = 1, **kwargs) -> SkillContext:
    args = {"action": action, **kwargs}
    return SkillContext(
        trigger="tool_call", user_id=user_id, channel_id=100,
        tool_name="manage_reminder", args=args,
    )


class TestReminderCreate:

    @pytest.mark.asyncio
    @patch("mochi.skills.reminder.handler.create_reminder", return_value=42)
    async def test_create_success(self, mock_create):
        skill = ReminderSkill()
        ctx = _make_ctx("create", message="Call mom", remind_at="2026-04-14T10:00:00")
        result = await skill.execute(ctx)
        assert result.success is True
        assert "#42" in result.output
        assert "Call mom" in result.output
        mock_create.assert_called_once_with(1, 100, "Call mom", "2026-04-14T10:00:00")

    @pytest.mark.asyncio
    async def test_create_missing_message(self):
        skill = ReminderSkill()
        ctx = _make_ctx("create", remind_at="2026-04-14T10:00:00")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "message" in result.output.lower()

    @pytest.mark.asyncio
    async def test_create_missing_remind_at(self):
        skill = ReminderSkill()
        ctx = _make_ctx("create", message="Something")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "remind_at" in result.output.lower()


class TestReminderList:

    @pytest.mark.asyncio
    @patch("mochi.skills.reminder.handler.get_pending_reminders", return_value=[])
    async def test_list_empty(self, mock_get):
        skill = ReminderSkill()
        ctx = _make_ctx("list")
        result = await skill.execute(ctx)
        assert "No pending reminders" in result.output

    @pytest.mark.asyncio
    @patch("mochi.skills.reminder.handler.get_pending_reminders")
    async def test_list_shows_reminders(self, mock_get):
        mock_get.return_value = [
            {"id": 1, "user_id": 1, "channel_id": 100,
             "message": "Buy groceries", "remind_at": "2026-04-14T09:00:00"},
            {"id": 2, "user_id": 1, "channel_id": 100,
             "message": "Workout", "remind_at": "2026-04-14T18:00:00"},
            {"id": 3, "user_id": 999, "channel_id": 200,
             "message": "Other user", "remind_at": "2026-04-14T12:00:00"},
        ]
        skill = ReminderSkill()
        ctx = _make_ctx("list", user_id=1)
        result = await skill.execute(ctx)
        assert "2 reminders" in result.output
        assert "Buy groceries" in result.output
        assert "Workout" in result.output
        # Other user's reminder should not appear
        assert "Other user" not in result.output


class TestReminderDelete:

    @pytest.mark.asyncio
    @patch("mochi.skills.reminder.handler.mark_reminder_fired")
    async def test_delete_success(self, mock_fire):
        skill = ReminderSkill()
        ctx = _make_ctx("delete", reminder_id="5")
        result = await skill.execute(ctx)
        assert result.success is True
        assert "#5" in result.output
        mock_fire.assert_called_once_with(5)

    @pytest.mark.asyncio
    async def test_delete_missing_id(self):
        skill = ReminderSkill()
        ctx = _make_ctx("delete")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "reminder_id" in result.output.lower()


class TestReminderUnknown:

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        skill = ReminderSkill()
        ctx = _make_ctx("snooze")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "Unknown action" in result.output
