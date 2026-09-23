"""Habit skill — DB queries.

Canonical source for habit CRUD and check-in logic.
Other modules should import from here.
"""

from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ


def add_habit(user_id: int, name: str, frequency: str,
              category: str = "", importance: str = "normal",
              context: str = "") -> int:
    """Create a new habit. Returns the habit id.

    frequency: "daily:N" (N times/day) or "weekly:N" (N times/week)
               or "weekly_on:DAY,...:N".
    importance: "important" or "normal".
    context: descriptive note (e.g. "morning and evening, after meals").
    """
    return create_or_reactivate_habit(
        user_id, name, frequency, category, importance, context,
    )[0]


def create_or_reactivate_habit(
    user_id: int, name: str, frequency: str, category: str = "",
    importance: str = "normal", context: str = "",
) -> tuple[int, bool]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, active FROM habits WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
        if existing is not None:
            if existing["active"]:
                raise ValueError(name)
            conn.execute(
                "UPDATE habits SET frequency = ?, category = ?, importance = ?, "
                "context = ?, active = 1, paused_until = NULL, snoozed_until = NULL "
                "WHERE id = ? AND user_id = ?",
                (frequency, category, importance, context, existing["id"], user_id),
            )
            conn.commit()
            return existing["id"], True
        cursor = conn.execute(
            "INSERT INTO habits (user_id, name, frequency, category, "
            "importance, context, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, frequency, category, importance, context,
             datetime.now(TZ).isoformat()),
        )
        conn.commit()
        return int(cursor.lastrowid), False
    finally:
        conn.close()


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
        "UPDATE habits SET active = 0 "
        "WHERE id = ? AND user_id = ? AND active = 1",
        (habit_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def update_habit(user_id: int, habit_id: int, **fields) -> bool:
    """Update mutable fields on a habit. Returns True if updated.

    Allowed fields: name, context, importance, frequency.
    """
    return mutate_habit(user_id, habit_id, **fields) == "updated"


def mutate_habit(user_id: int, habit_id: int, **fields) -> str:
    allowed = {"name", "context", "importance", "frequency", "category",
               "paused_until", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM habits WHERE id = ? AND user_id = ?",
            (habit_id, user_id),
        ).fetchone()
        if current is None or (not current["active"] and updates != {"active": 0}):
            return "not_found"
        if all(current[key] == value for key, value in updates.items()):
            return "unchanged"
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE habits SET {assignments} WHERE id = ? AND user_id = ?",
            (*updates.values(), habit_id, user_id),
        )
        conn.commit()
        return "updated"
    finally:
        conn.close()


def record_habit_progress(
    habit_id: int, user_id: int, period: str, *, count: int | None = None,
    total: int | None = None, note: str = "",
) -> tuple[int, int]:
    """Return committed total and added count from one owner-scoped transaction."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM habits WHERE id = ? AND user_id = ? AND active = 1",
            (habit_id, user_id),
        ).fetchone() is None:
            raise LookupError(habit_id)
        current = conn.execute(
            "SELECT COUNT(*) FROM habit_logs WHERE habit_id = ? "
            "AND user_id = ? AND period = ?",
            (habit_id, user_id, period),
        ).fetchone()[0]
        if total is not None and total < current:
            raise ValueError(current)
        added = total - current if total is not None else count
        conn.executemany(
            "INSERT INTO habit_logs (habit_id, user_id, note, logged_at, period) "
            "VALUES (?, ?, ?, ?, ?)",
            [(habit_id, user_id, note, datetime.now(TZ).isoformat(), period)
             for _ in range(added)],
        )
        conn.commit()
        return current + added, added
    finally:
        conn.close()


def undo_latest_habit_checkin(habit_id: int, user_id: int, period: str) -> int | None:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM habits WHERE id = ? AND user_id = ? AND active = 1",
            (habit_id, user_id),
        ).fetchone() is None:
            raise LookupError(habit_id)
        latest = conn.execute(
            "SELECT id FROM habit_logs WHERE habit_id = ? AND user_id = ? "
            "AND period = ? ORDER BY logged_at DESC, id DESC LIMIT 1",
            (habit_id, user_id, period),
        ).fetchone()
        if latest is None:
            return None
        conn.execute("DELETE FROM habit_logs WHERE id = ?", (latest["id"],))
        remaining = conn.execute(
            "SELECT COUNT(*) FROM habit_logs WHERE habit_id = ? "
            "AND user_id = ? AND period = ?",
            (habit_id, user_id, period),
        ).fetchone()[0]
        conn.commit()
        return remaining
    finally:
        conn.close()


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
        from mochi.admin.admin_db import get_system_config
        logical_now = now - timedelta(days=1) if now.hour < get_system_config("MAINTENANCE_HOUR") else now
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
