"""Habit skill — DB queries.

Canonical source for habit CRUD and check-in logic.
Other modules should import from here.
"""

from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ, logical_today, logical_days_ago, MAINTENANCE_HOUR


def add_habit(user_id: int, name: str, frequency: str,
              category: str = "", importance: str = "normal",
              context: str = "") -> int:
    """Create a new habit. Returns the habit id.

    frequency: "daily:N" (N times/day) or "weekly:N" (N times/week)
               or "weekly_on:DAY,...:N".
    importance: "important" or "normal".
    context: descriptive note (e.g. "morning and evening, after meals").
    """
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO habits (user_id, name, frequency, category, "
        "importance, context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, frequency, category, importance, context, now),
    )
    habit_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return habit_id


def list_habits(user_id: int, active_only: bool = True) -> list[dict]:
    """Return habits for a user."""
    conn = _connect()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM habits WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def deactivate_habit(user_id: int, habit_id: int) -> bool:
    """Deactivate (soft-delete) a habit. Returns True if updated."""
    conn = _connect()
    cursor = conn.execute(
        "UPDATE habits SET active = 0 WHERE id = ? AND user_id = ?",
        (habit_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def update_habit(habit_id: int, **fields) -> bool:
    """Update mutable fields on a habit. Returns True if updated.

    Allowed fields: name, context, importance, frequency.
    """
    allowed = {"name", "context", "importance", "frequency"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [habit_id]
    conn = _connect()
    cursor = conn.execute(
        f"UPDATE habits SET {set_clause} WHERE id = ? AND active = 1",
        values,
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def checkin_habit(habit_id: int, user_id: int, period: str,
                  note: str = "") -> int:
    """Record a check-in for a habit. Returns the log id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO habit_logs (habit_id, user_id, note, logged_at, period) "
        "VALUES (?, ?, ?, ?, ?)",
        (habit_id, user_id, note, now, period),
    )
    log_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return log_id


def get_habit_checkins(habit_id: int, period: str) -> list[dict]:
    """Return check-in logs for a habit in a specific period."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM habit_logs WHERE habit_id = ? AND period = ? "
        "ORDER BY logged_at",
        (habit_id, period),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_habit_checkin(log_id: int) -> bool:
    """Delete a specific habit check-in log by its id. Returns True if deleted."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM habit_logs WHERE id = ?", (log_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_habit_stats(habit_id: int, periods: list[str]) -> dict:
    """Return check-in counts keyed by period for a habit.

    periods: list of period strings, e.g. ["2026-02-22", "2026-02-21"].
    Returns {period: count}.
    """
    if not periods:
        return {}
    conn = _connect()
    placeholders = ",".join("?" for _ in periods)
    rows = conn.execute(
        f"SELECT period, COUNT(*) as cnt FROM habit_logs "
        f"WHERE habit_id = ? AND period IN ({placeholders}) "
        f"GROUP BY period",
        [habit_id] + periods,
    ).fetchall()
    conn.close()
    return {r["period"]: r["cnt"] for r in rows}


def get_all_habit_checkins_for_period(user_id: int, period: str) -> dict[int, int]:
    """Return {habit_id: checkin_count} for all habits for a given period."""
    conn = _connect()
    rows = conn.execute(
        "SELECT habit_id, COUNT(*) as cnt FROM habit_logs "
        "WHERE user_id = ? AND period = ? GROUP BY habit_id",
        (user_id, period),
    ).fetchall()
    conn.close()
    return {r["habit_id"]: r["cnt"] for r in rows}


def get_latest_habit_checkins_for_period(user_id: int, period: str) -> dict[int, str]:
    """Return {habit_id: latest_logged_at} for all habits in a period."""
    conn = _connect()
    rows = conn.execute(
        "SELECT habit_id, MAX(logged_at) as latest FROM habit_logs "
        "WHERE user_id = ? AND period = ? GROUP BY habit_id",
        (user_id, period),
    ).fetchall()
    conn.close()
    return {r["habit_id"]: r["latest"] for r in rows}


def get_habit_streak(
    habit_id: int, cycle: str, target: int,
    allowed_days: set[int] | None = None, max_lookback: int = 90,
) -> int:
    """Compute current streak (consecutive completed periods) for a habit.

    For daily habits: walks backwards from yesterday, skipping non-allowed days.
    For weekly habits: walks backwards from last week.
    Returns 0 if the most recent eligible period was missed.
    """
    now = datetime.now(TZ)
    if cycle == "daily":
        # logical 起点：roll back if before MAINTENANCE_HOUR
        logical_now = now - timedelta(days=1) if now.hour < MAINTENANCE_HOUR else now
        periods = []
        for i in range(1, max_lookback + 1):
            d = logical_now - timedelta(days=i)
            if allowed_days is not None and d.weekday() not in allowed_days:
                continue
            periods.append(d.strftime("%Y-%m-%d"))
    else:
        # wall-clock 故意：ISO 周边界在 Mon 00:00，与 maintenance window (0-3) 不冲突
        periods = []
        for i in range(1, max_lookback // 7 + 1):
            d = now - timedelta(weeks=i)
            periods.append(d.strftime("%G-W%V"))

    if not periods:
        return 0

    stats = get_habit_stats(habit_id, periods)
    streak = 0
    for p in periods:
        if stats.get(p, 0) >= target:
            streak += 1
        else:
            break
    return streak


def pause_habit(user_id: int, habit_id: int, until_date: str) -> bool:
    """Pause a habit until the given ISO date (inclusive). Returns True if updated."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE habits SET paused_until = ? WHERE id = ? AND user_id = ? AND active = 1",
        (until_date, habit_id, user_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def resume_habit(user_id: int, habit_id: int) -> bool:
    """Resume a paused habit (clear paused_until). Returns True if updated."""
    conn = _connect()
    cur = conn.execute(
        "UPDATE habits SET paused_until = NULL WHERE id = ? AND user_id = ? AND active = 1",
        (habit_id, user_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok
