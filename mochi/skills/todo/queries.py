"""Todo domain queries — nudge scheduling for diary integration.

Canonical source for todo domain logic used by diary status refresh.
"""

from mochi.db import _connect


def get_visible_todos(today_str: str) -> list[dict]:
    """Return pending todos visible in diary: due today, overdue, or no date.

    Future todos (nudge_date > today) are excluded.
    """
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, task, nudge_date FROM todos "
        "WHERE done = 0 AND (nudge_date IS NULL OR nudge_date <= ?) "
        "ORDER BY nudge_date IS NULL, nudge_date, id",
        (today_str,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
