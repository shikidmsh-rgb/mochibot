"""E2E tests for reminder delivery via check_and_fire_reminders."""

import pytest

from mochi.db import create_reminder, get_pending_reminders, mark_reminder_fired
from mochi.main import check_and_fire_reminders
from tests.e2e.fake_transport import FakeTransport


class TestReminderDelivery:

    @pytest.mark.asyncio
    async def test_due_reminder_fires(self):
        """Past-due reminder is delivered and marked fired."""
        transport = FakeTransport()
        rid = create_reminder(1, 100, "Stretch break", "2020-01-01T00:00:00")

        fired = await check_and_fire_reminders(transport)

        assert fired == 1
        assert len(transport.sent_messages) == 1
        uid, text = transport.sent_messages[0]
        assert uid == 100
        assert "Stretch break" in text

        # Reminder should now be marked as fired
        pending = get_pending_reminders()
        assert not any(r["id"] == rid for r in pending)

    @pytest.mark.asyncio
    async def test_multiple_reminders_fire(self):
        """Multiple due reminders all fire in one pass."""
        transport = FakeTransport()
        create_reminder(1, 100, "First", "2020-01-01T00:00:00")
        create_reminder(1, 100, "Second", "2020-06-15T12:00:00")

        fired = await check_and_fire_reminders(transport)

        assert fired == 2
        assert len(transport.sent_messages) == 2

    @pytest.mark.asyncio
    async def test_no_pending_reminders(self):
        """No reminders → nothing fires."""
        transport = FakeTransport()

        fired = await check_and_fire_reminders(transport)

        assert fired == 0
        assert len(transport.sent_messages) == 0

    @pytest.mark.asyncio
    async def test_already_fired_reminder_skipped(self):
        """Manually fired reminder doesn't fire again."""
        transport = FakeTransport()
        rid = create_reminder(1, 100, "Done", "2020-01-01T00:00:00")
        mark_reminder_fired(rid)

        fired = await check_and_fire_reminders(transport)

        assert fired == 0
        assert len(transport.sent_messages) == 0
