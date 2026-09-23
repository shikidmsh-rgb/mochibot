"""Todo skill — DB queries.

Canonical source for todo CRUD and domain queries.
"""

from datetime import datetime
import unicodedata

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
        "UPDATE todos SET done = 1, completed_at = ? "
        "WHERE id = ? AND user_id = ? AND done = 0",
        (now, todo_id, user_id),
    )
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def normalize_todo_match(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def find_todos_by_exact_match(
    user_id: int, match: str, *, done: bool | None = None,
) -> list[dict]:
    normalized = normalize_todo_match(match)
    return [
        todo for todo in get_todos(user_id, include_done=True)
        if (done is None or todo["done"] is done)
        and normalize_todo_match(todo["task"]) == normalized
    ]


def set_todo_done(user_id: int, todo_id: int, done: bool) -> str:
    return mutate_todo(
        user_id, todo_id, done=int(done),
        completed_at=datetime.now(TZ).isoformat() if done else None,
    )


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
    return mutate_todo(user_id, todo_id, **{
        key: value for key, value in fields.items() if key in {"task", "nudge_date"}
    }) == "updated"


def mutate_todo(user_id: int, todo_id: int, **fields) -> str:
    allowed = {"task", "nudge_date", "done", "completed_at"}
    to_set = {k: v for k, v in fields.items() if k in allowed}
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM todos WHERE id = ? AND user_id = ?", (todo_id, user_id),
        ).fetchone()
        if current is None:
            return "not_found"
        if "done" in to_set and current["done"] == to_set["done"]:
            return "unchanged"
        if all(current[key] == value for key, value in to_set.items()):
            return "unchanged"
        assignments = ", ".join(f"{key} = ?" for key in to_set)
        conn.execute(
            f"UPDATE todos SET {assignments} WHERE id = ? AND user_id = ?",
            (*to_set.values(), todo_id, user_id),
        )
        conn.commit()
        return "updated"
    finally:
        conn.close()


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
