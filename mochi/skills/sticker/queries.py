"""Sticker skill — DB queries.

Canonical source for sticker registry CRUD.
"""

from datetime import datetime

from mochi.db import _connect
from mochi.config import TZ


def save_sticker(user_id: int, file_id: str, set_name: str = "",
                 emoji: str = "", tags: str = "") -> int | None:
    """Save a learned sticker. Returns rowid on success, None if duplicate."""
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO sticker_registry "
            "(user_id, file_id, set_name, emoji, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, file_id, set_name, emoji, tags,
             datetime.now(TZ).isoformat()),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    finally:
        conn.close()


def get_stickers_by_tag(tag: str, user_id: int = 0) -> list[dict]:
    """Find stickers whose tags contain the given keyword."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, file_id, set_name, emoji, tags, created_at "
        "FROM sticker_registry WHERE tags LIKE ? "
        "AND (user_id = ? OR user_id = 0)",
        (f"%{tag}%", user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_sticker_count(user_id: int = 0) -> int:
    """Count learned stickers for a user."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) FROM sticker_registry "
        "WHERE user_id = ? OR user_id = 0",
        (user_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def delete_sticker(file_id: str) -> bool:
    """Delete a sticker by file_id. Returns True if deleted."""
    conn = _connect()
    cur = conn.execute(
        "DELETE FROM sticker_registry WHERE file_id = ?", (file_id,)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0
