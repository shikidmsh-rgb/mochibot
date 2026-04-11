"""Habit observer — read-only habit progress data for heartbeat.

Collects active habit status from DB for heartbeat observation.
No side effects — pure read.
"""

import logging
from datetime import datetime

from mochi.config import TZ, OWNER_USER_ID, logical_today
from mochi.observers.base import Observer
from mochi.skills.habit.logic import parse_frequency, get_allowed_days
from mochi.db import (
    list_habits,
    get_habit_checkins,
    get_habit_streak,
)

log = logging.getLogger(__name__)


class HabitObserver(Observer):

    async def observe(self) -> dict:
        user_id = OWNER_USER_ID
        if not user_id:
            return {}

        habits = list_habits(user_id)
        if not habits:
            return {"items": [], "total_count": 0, "incomplete_count": 0}

        now = datetime.now(TZ)
        today = logical_today(now)
        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()

        items = []
        incomplete = 0

        for h in habits:
            # Skip paused habits
            paused_until = h.get("paused_until")
            if paused_until and paused_until >= today:
                continue

            # Skip snoozed habits
            snoozed_until = h.get("snoozed_until")
            if snoozed_until:
                try:
                    snooze_dt = datetime.fromisoformat(snoozed_until)
                    if snooze_dt.tzinfo is None:
                        snooze_dt = snooze_dt.replace(tzinfo=TZ)
                    if now < snooze_dt:
                        continue
                except (ValueError, TypeError):
                    pass

            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week
            checkins = get_habit_checkins(h["id"], period)
            done = len(checkins)
            remaining = max(0, target - done)

            allowed = get_allowed_days(h["frequency"])
            active_today = allowed is None or weekday in allowed

            # Last checkin time
            last_checkin_at = checkins[-1]["logged_at"] if checkins else None

            # Streak (skip for important/task-type habits)
            streak = 0
            if h["importance"] != "important":
                streak = get_habit_streak(h["id"], cycle, target, allowed)

            items.append({
                "id": h["id"],
                "name": h["name"],
                "cycle": cycle,
                "target": target,
                "done": done,
                "remaining": remaining,
                "importance": h.get("importance", "normal"),
                "category": h.get("category", ""),
                "context": h.get("context", ""),
                "active_today": active_today,
                "last_checkin_at": last_checkin_at,
                "streak": streak,
            })

            if remaining > 0 and active_today:
                incomplete += 1

        return {
            "items": items,
            "total_count": len(items),
            "incomplete_count": incomplete,
        }

    def has_delta(self, prev: dict, curr: dict) -> bool:
        """Delta on incomplete count change (checkin happened or new day)."""
        return prev.get("incomplete_count") != curr.get("incomplete_count")
