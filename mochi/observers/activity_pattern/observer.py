"""Activity Pattern Observer — conversation pattern detection from SQLite.

Zero LLM calls. Reads per-day message counts from the database and detects
behavioral patterns:
  - "Talked a lot yesterday, silent today" → might be struggling
  - "Daily average much lower than usual" → unusual quiet period
  - "Chat always peaks in evenings" → personalization data

This is the open-source equivalent of desktop activity sensing —
pure conversation data tells us a lot about user state.
"""

import logging
import statistics
from datetime import datetime, timezone, timedelta

from mochi.observers.base import Observer
from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Minimum messages on a day to consider it "active"
_ACTIVE_DAY_THRESHOLD = 3

# How much lower than average before flagging as "unusually quiet"
_QUIET_RATIO = 0.3  # today's count < 30% of average → quiet signal


class ActivityPatternObserver(Observer):
    """Detects conversation patterns over the last 7 days. No external API."""

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_daily_message_counts

        user_id = OWNER_USER_ID
        if not user_id:
            return {}

        # Get last 7 days (includes today, always 7 entries)
        daily = get_daily_message_counts(user_id, days=7)
        if not daily:
            return {}

        today_entry = daily[-1]
        yesterday_entry = daily[-2] if len(daily) >= 2 else None

        today_count = today_entry["count"]
        yesterday_count = yesterday_entry["count"] if yesterday_entry else 0

        # Past 7 days (excluding today for baseline)
        past_counts = [d["count"] for d in daily[:-1]]
        active_days = sum(1 for c in past_counts if c >= _ACTIVE_DAY_THRESHOLD)

        # Average over days that had at least some activity (avoids skewing by
        # totally silent days e.g. before user started using the bot)
        active_counts = [c for c in past_counts if c > 0]
        daily_avg = round(statistics.mean(active_counts), 1) if active_counts else 0.0
        daily_avg_7d = round(statistics.mean(past_counts), 1)

        result = {
            "today_messages": today_count,
            "yesterday_messages": yesterday_count,
            "daily_avg_7d": daily_avg_7d,
            "active_days_7d": active_days,
        }

        # Include the 7-day trend (useful for LLM)
        result["weekly_trend"] = [
            {"date": d["date"], "count": d["count"]} for d in daily
        ]

        # Detect patterns
        signals = []

        # 1. Big drop: yesterday high, today low
        if yesterday_count >= _ACTIVE_DAY_THRESHOLD and today_count == 0:
            signals.append("silent_after_active_day")

        # 2. Unusually quiet vs personal baseline
        if daily_avg > 0 and today_count < (daily_avg * _QUIET_RATIO):
            if today_count == 0:
                signals.append("unusually_quiet")
            else:
                signals.append("below_average_activity")

        # 3. Multi-day silence (no messages for 2+ consecutive days including today)
        recent_zero_days = 0
        for d in reversed(daily):
            if d["count"] == 0:
                recent_zero_days += 1
            else:
                break
        if recent_zero_days >= 2:
            signals.append(f"silent_{recent_zero_days}_days")

        # 4. High engagement today
        if daily_avg > 0 and today_count > daily_avg * 2:
            signals.append("high_engagement_today")

        if signals:
            result["signals"] = signals

        return result
