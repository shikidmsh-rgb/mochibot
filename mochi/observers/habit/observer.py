"""Habit Observer — reads habit streaks and completion status from SQLite.

No external API needed. Data is written by the habit skill and read here.

User ID: uses OWNER_USER_ID from config (single-user system).
"""

import logging

from mochi.observers.base import Observer

log = logging.getLogger(__name__)


class HabitObserver(Observer):
    """Reads habit tracking data every 60 minutes."""

    async def observe(self) -> dict:
        # Import here to avoid circular imports at module load time
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_habits_overview

        user_id = OWNER_USER_ID
        if not user_id:
            return {}  # No owner yet, skip silently

        habits = get_habits_overview(user_id)
        if not habits:
            return {}  # No habits defined — nothing to report

        active_habits = len(habits)
        logged_today = sum(1 for h in habits if h["logged_today"])
        due_today = [h["name"] for h in habits if not h["logged_today"]]

        # Top streaks (≥2 days, sorted descending)
        streaks = sorted(
            [{"name": h["name"], "streak_days": h["streak_days"]}
             for h in habits if h["streak_days"] >= 2],
            key=lambda x: x["streak_days"],
            reverse=True,
        )[:3]  # Top 3

        # Human-readable summary
        parts = []
        if active_habits:
            parts.append(f"{logged_today}/{active_habits} habits done today")
        if streaks:
            top = streaks[0]
            parts.append(f"{top['streak_days']}-day {top['name']} streak")
        summary = ", ".join(parts) if parts else ""

        result: dict = {
            "active_habits": active_habits,
            "logged_today": logged_today,
            "summary": summary,
        }
        if due_today:
            result["due_today"] = due_today
        if streaks:
            result["streaks"] = streaks

        return result
