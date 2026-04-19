"""Todo Observer — surfaces active todo count for heartbeat awareness."""

import logging

from mochi.observers.base import Observer

log = logging.getLogger(__name__)


class TodoObserver(Observer):
    """Exposes the count of active (not done) todos."""

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Only trigger Think when count actually changes."""
        return prev.get("active_count") != curr.get("active_count")

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.skills.todo.queries import get_active_todo_count

        user_id = OWNER_USER_ID
        if user_id is None:
            return {}

        count = get_active_todo_count(user_id)
        return {"active_count": count}
