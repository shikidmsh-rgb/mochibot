"""Reminder skill — DB queries.

Canonical source for reminder CRUD. Other modules should import from here.
"""

from datetime import datetime, timedelta

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


def delete_reminder(reminder_id: int) -> bool:
    """Hard-delete a reminder. Returns True if a row was removed."""
    conn = _connect()
    cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


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


def get_upcoming_reminders(user_id: int, hours_ahead: int = 2) -> list[dict]:
    """Get reminders due within the next N hours."""
    now = datetime.now(TZ)
    cutoff = (now + timedelta(hours=hours_ahead)).isoformat()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, message, remind_at FROM reminders WHERE user_id = ? AND fired = 0 AND remind_at <= ?",
        (user_id, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_pending_reminders(days_ahead: int = 7) -> list[dict]:
    """Return all unfired reminders within the next N days (for heap loading)."""
    cutoff = (datetime.now(TZ) + timedelta(days=days_ahead)).isoformat()
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, channel_id, message, remind_at, recurrence "
        "FROM reminders WHERE fired = 0 AND remind_at <= ? ORDER BY remind_at ASC",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_reminder_diagnostic_section() -> str:
    """Return a formatted diagnostics section for the diagnostic report."""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, user_id, message, remind_at, recurrence "
            "FROM reminders WHERE fired = 0 ORDER BY remind_at ASC",
        ).fetchall()
        conn.close()
        lines = ["--- Reminder State ---"]
        lines.append(f"Pending (unfired): {len(rows)}")
        for r in rows:
            r = dict(r)
            msg = r["message"]
            msg_preview = (msg[:50] + "...") if len(msg) > 50 else msg
            rec = f" recurrence={r['recurrence']}" if r.get("recurrence") else ""
            lines.append(
                f"  #{r['id']} user={r['user_id']} "
                f"remind_at={r['remind_at']} message={msg_preview}{rec}"
            )
        if not rows:
            lines.append("  (none)")
        return "\n".join(lines)
    except Exception as e:
        return f"--- Reminder State ---\n(query failed: {e})"
