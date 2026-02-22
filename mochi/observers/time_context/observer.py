"""Time Context Observer — pure code awareness of time, date, and holidays.

No external API. Provides the heartbeat with rich temporal context:
- Current date, weekday, time-of-day
- Minutes since last user message
- Whether today is a weekend or holiday (extensible)

Why this matters: LLMs don't know "now" unless told. This observer gives
structured time data so the Think step can reason about context like
"it's 2am on a Sunday" or "user hasn't talked in 6 hours".
"""

from datetime import datetime, timezone, timedelta

from mochi.observers.base import Observer
from mochi.config import TIMEZONE_OFFSET_HOURS
from mochi.db import get_last_user_message_time

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Static holiday list — easy to extend or replace with external data
# Format: (month, day) tuples. Add your country's holidays as needed.
_FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 1),    # New Year
    (12, 25),  # Christmas
    (12, 31),  # New Year's Eve
    # Add more: (2, 14) for Valentine's, etc.
}


def _time_of_day_label(hour: int) -> str:
    """Map hour -> human-readable time-of-day label."""
    if 5 <= hour < 9:
        return "early_morning"
    if 9 <= hour < 12:
        return "morning"
    if 12 <= hour < 14:
        return "lunch"
    if 14 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 21:
        return "evening"
    if 21 <= hour < 24:
        return "night"
    return "late_night"  # 0-4


def _is_holiday(dt: datetime) -> tuple[bool, str]:
    """Check if date is a known holiday. Returns (is_holiday, holiday_name)."""
    key = (dt.month, dt.day)
    names = {
        (1, 1): "New Year's Day",
        (12, 25): "Christmas",
        (12, 31): "New Year's Eve",
    }
    if key in _FIXED_HOLIDAYS:
        return True, names.get(key, "Holiday")
    return False, ""


class TimeContextObserver(Observer):
    """Provides time awareness every heartbeat tick (no external calls)."""

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID

        now = datetime.now(TZ)
        user_id = OWNER_USER_ID

        # Core time data
        result = {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": now.strftime("%A"),
            "hour": now.hour,
            "minute": now.minute,
            "time_of_day": _time_of_day_label(now.hour),
            "is_weekend": now.weekday() >= 5,
        }

        # Holiday check
        is_hol, hol_name = _is_holiday(now)
        if is_hol:
            result["is_holiday"] = True
            result["holiday_name"] = hol_name
        else:
            result["is_holiday"] = False

        # Silence duration (minutes since last user message)
        if user_id:
            last_msg_time = get_last_user_message_time(user_id)
            if last_msg_time:
                try:
                    last_dt = datetime.fromisoformat(last_msg_time)
                    # Handle timezone-naive stored timestamps
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=TZ)
                    silence_mins = (now - last_dt).total_seconds() / 60
                    result["silence_minutes"] = round(silence_mins)
                    result["silence_hours"] = round(silence_mins / 60, 1)
                except (ValueError, TypeError):
                    pass

        return result
