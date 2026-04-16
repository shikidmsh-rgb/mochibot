"""E2E tests for reminder delivery via reminder_timer.

The reminder_timer module uses a long-running loop (reminder_loop) with a
send callback.  These tests exercise the underlying DB + callback contract:
get_pending_reminders / get_next_pending_reminder → fire → mark_reminder_fired.
"""

import pytest

from mochi.skills.reminder.queries import (
    create_reminder,
    get_pending_reminders,
    get_next_pending_reminder,
    mark_reminder_fired,
)
from mochi.reminder_timer import set_send_callback


class TestReminderDelivery:

    @pytest.mark.asyncio
    async def test_due_reminder_fires(self):
        """Past-due reminder is delivered and marked fired."""
        sent = []

        async def _capture(user_id, text):
            sent.append((user_id, text))

        set_send_callback(_capture)

        rid = create_reminder(1, 100, "Stretch break", "2020-01-01T00:00:00")

        pending = get_pending_reminders()
        assert any(r["id"] == rid for r in pending)

        # Simulate what reminder_loop does: fire via callback, mark fired
        for r in pending:
            await _capture(r["channel_id"], f"\u23f0 {r['message']}")
            mark_reminder_fired(r["id"])

        assert len(sent) == 1
        uid, text = sent[0]
        assert uid == 100
        assert "Stretch break" in text

        # Reminder should now be marked as fired
        pending = get_pending_reminders()
        assert not any(r["id"] == rid for r in pending)

    @pytest.mark.asyncio
    async def test_multiple_reminders_fire(self):
        """Multiple due reminders all fire in one pass."""
        sent = []

        async def _capture(user_id, text):
            sent.append((user_id, text))

        create_reminder(1, 100, "First", "2020-01-01T00:00:00")
        create_reminder(1, 100, "Second", "2020-06-15T12:00:00")

        pending = get_pending_reminders()
        for r in pending:
            await _capture(r["channel_id"], f"\u23f0 {r['message']}")
            mark_reminder_fired(r["id"])

        assert len(sent) == 2

    @pytest.mark.asyncio
    async def test_no_pending_reminders(self):
        """No reminders → nothing fires."""
        pending = get_pending_reminders()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_already_fired_reminder_skipped(self):
        """Manually fired reminder doesn't appear in pending."""
        rid = create_reminder(1, 100, "Done", "2020-01-01T00:00:00")
        mark_reminder_fired(rid)

        pending = get_pending_reminders()
        assert not any(r["id"] == rid for r in pending)

    @pytest.mark.asyncio
    async def test_get_next_pending_returns_earliest(self):
        """get_next_pending_reminder returns the soonest unfired reminder."""
        create_reminder(1, 100, "Later", "2020-06-01T00:00:00")
        create_reminder(1, 100, "Earlier", "2020-01-01T00:00:00")

        nxt = get_next_pending_reminder()
        assert nxt is not None
        assert nxt["message"] == "Earlier"
