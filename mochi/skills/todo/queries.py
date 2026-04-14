"""Todo skill — DB queries.

Canonical source for todo CRUD and domain queries.
"""

from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ


def create_todo(user_id: int, task: str,
                nudge_date: str | None = None) -> int:
    """Add a todo item. Returns the new todo id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO todos (user_id, task, created_at, nudge_date)"
        " VALUES (?, ?, ?, ?)",
        (user_id, task, now, nudge_date),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def get_todos(user_id: int, include_done: bool = False) -> list[dict]:
    """Return todos for a user."""
    conn = _connect()
    conditions = ["user_id = ?"]
    params: list = [user_id]
    if not include_done:
        conditions.append("done = 0")
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, task, done, created_at, nudge_date FROM todos"
        f" WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    conn.close()
    return [
        {"id": r["id"], "task": r["task"], "done": bool(r["done"]),
         "created_at": r["created_at"], "nudge_date": r["nudge_date"]}
        for r in rows
    ]


def complete_todo(user_id: int, todo_id: int) -> bool:
    """Mark a todo as done. Returns True if updated."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "UPDATE todos SET done = 1, completed_at = ? WHERE id = ? AND user_id = ?",
        (now, todo_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_todo(user_id: int, todo_id: int) -> bool:
    """Delete a todo. Returns True if deleted."""
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def update_todo(user_id: int, todo_id: int, **fields) -> bool:
    """Update mutable fields on a todo. Returns True if updated.

    Supported fields: task, nudge_date.
    """
    allowed = {"task", "nudge_date"}
    to_set = {k: v for k, v in fields.items() if k in allowed}
    if not to_set:
        return False
    set_clause = ", ".join(f"{k} = ?" for k in to_set)
    params = list(to_set.values()) + [todo_id, user_id]
    conn = _connect()
    cursor = conn.execute(
        f"UPDATE todos SET {set_clause} WHERE id = ? AND user_id = ?", params
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def purge_done_todos(days: int = 30) -> int:
    """Delete completed todos older than *days*. Returns count deleted."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM todos WHERE done = 1 AND completed_at IS NOT NULL AND completed_at < ?",
        (cutoff,),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


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


def get_active_todo_count(user_id: int) -> int:
    """Count active (not done) todos for a user."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM todos WHERE user_id = ? AND done = 0",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0
