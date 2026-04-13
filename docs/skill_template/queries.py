"""My skill — DB queries."""

from datetime import datetime

from mochi.db import _connect
from mochi.config import TZ


def create_item(user_id: int, content: str) -> int:
    """Add an item. Returns the new item id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO my_items (user_id, content, created_at) VALUES (?, ?, ?)",
        (user_id, content, now),
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_items(user_id: int) -> list[dict]:
    """Get all items for a user."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content, created_at FROM my_items WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_item(user_id: int, item_id: int) -> bool:
    """Delete an item. Returns True if deleted."""
    conn = _connect()
    cur = conn.execute(
        "DELETE FROM my_items WHERE id = ? AND user_id = ?",
        (item_id, user_id),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted
