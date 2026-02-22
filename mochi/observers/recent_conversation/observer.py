"""Recent Conversation Observer — last N message rounds from SQLite.

Zero external calls. Gives the heartbeat Think step conversational context
so it can reason about *what* was discussed, not just *how long ago*.

Without this: Think sees "silence_hours=4" — can't tell if user was stressed,
excited, or just busy. With this: Think sees the last 10 messages and can
craft a genuinely contextual check-in.

Token efficiency: messages are truncated to MAX_CHARS_PER_MSG to prevent
the observation from dominating the Think prompt.
"""

import logging
from datetime import datetime, timezone, timedelta

from mochi.observers.base import Observer
from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# Max characters to include per message (keeps tokens in check)
MAX_CHARS_PER_MSG = 200

# Number of messages to include (10 rounds ≈ 20 messages)
MSG_LIMIT = 20


def _relative_time(ts_str: str, now: datetime) -> str:
    """Convert ISO timestamp to relative label: 'just now', '2h ago', etc."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        delta = now - dt
        minutes = int(delta.total_seconds() / 60)
        if minutes < 2:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"
    except (ValueError, TypeError):
        return ""


class RecentConversationObserver(Observer):
    """Provides last ~10 conversation rounds to the heartbeat. No external API."""

    async def observe(self) -> dict:
        from mochi.config import OWNER_USER_ID
        from mochi.db import get_recent_messages

        user_id = OWNER_USER_ID
        if not user_id:
            return {}

        messages = get_recent_messages(user_id, limit=MSG_LIMIT)
        if not messages:
            return {}

        now = datetime.now(TZ)

        # Build compact message list
        compact = []
        for m in messages:
            content = m.get("content", "")
            # Truncate long messages
            if len(content) > MAX_CHARS_PER_MSG:
                content = content[:MAX_CHARS_PER_MSG] + "…"

            entry: dict = {
                "role": m.get("role", ""),
                "content": content,
            }
            rel = _relative_time(m.get("created_at", ""), now)
            if rel:
                entry["when"] = rel

            compact.append(entry)

        result: dict = {
            "messages": compact,
            "count": len(compact),
        }

        # Convenience: last thing the user said (for quick LLM reference)
        user_msgs = [m for m in messages if m.get("role") == "user"]
        if user_msgs:
            last_user = user_msgs[-1]
            text = last_user.get("content", "")
            result["last_user_message"] = text[:MAX_CHARS_PER_MSG]
            result["last_user_message_when"] = _relative_time(
                last_user.get("created_at", ""), now
            )

        return result
