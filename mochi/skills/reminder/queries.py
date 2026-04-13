"""Reminder skill — DB queries.

Canonical source for reminder CRUD. Other modules should import from here.
"""

from datetime import datetime

from mochi.db import _connect
from mochi.config import TZ


def create_reminder(user_id: int, channel_id: int, message: str, remind_at: str) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (?, ?, ?, ?)",
        (user_id, channel_id, message, remind_at),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def get_pending_reminders() -> list[dict]:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, channel_id, message, remind_at FROM reminders WHERE fired = 0 AND remind_at <= ?",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_reminder_fired(reminder_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()


def get_next_pending_reminder() -> dict | None:
    """Return the earliest unfired reminder, or None."""
    conn = _connect()
    row = conn.execute(
        "SELECT id, user_id, channel_id, message, remind_at, recurrence "
        "FROM reminders WHERE fired = 0 ORDER BY remind_at ASC LIMIT 1",
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def reschedule_reminder(reminder_id: int, new_remind_at: str) -> None:
    """Update remind_at for a recurring reminder (reset fired to 0)."""
    conn = _connect()
    conn.execute(
        "UPDATE reminders SET remind_at = ?, fired = 0 WHERE id = ?",
        (new_remind_at, reminder_id),
    )
    conn.commit()
    conn.close()
