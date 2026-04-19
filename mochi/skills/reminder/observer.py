"""Reminder Observer — surfaces upcoming reminders for heartbeat awareness."""

import logging

from mochi.observers.base import Observer

log = logging.getLogger(__name__)


class ReminderObserver(Observer):
    """Exposes unfired reminders due within the next 2 hours."""

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.skills.reminder.queries import get_upcoming_reminders

        user_id = OWNER_USER_ID
        if user_id is None:
            return {}

        upcoming = get_upcoming_reminders(user_id, hours_ahead=2)
        if not upcoming:
            return {}

        return {
            "upcoming": [
                {"message": r["message"], "remind_at": r["remind_at"]}
                for r in upcoming
            ],
        }
