"""Meal skill — DB queries.

Canonical source for health_log CRUD used by the meal skill.
"""

from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ


def save_health_log(user_id: int, date: str, log_type: str, content: str,
                    source: str = "oura_daily", importance: int = 1,
                    metrics: str | None = None) -> int:
    """Insert or upsert a health log record.

    Upsert rule: same (user_id, date, type, source) -> UPDATE content/metrics/updated_at.
    Different source with same date+type -> INSERT (allows multiple sources to coexist).
    """
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    existing = conn.execute(
        "SELECT id FROM health_log "
        "WHERE user_id = ? AND date = ? AND type = ? AND source = ? LIMIT 1",
        (user_id, date, log_type, source),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE health_log SET content = ?, metrics = ?, importance = MAX(importance, ?), "
            "updated_at = ? WHERE id = ?",
            (content, metrics, importance, now, existing["id"]),
        )
        conn.commit()
        conn.close()
        return existing["id"]

    conn.execute(
        "INSERT INTO health_log (user_id, date, type, source, content, metrics, "
        "importance, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, date, log_type, source, content, metrics, importance, now, now),
    )
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return row_id


def query_health_log(user_id: int, types: list[str] | None = None,
                     days: int = 7, date: str | None = None,
                     limit: int = 200) -> list[dict]:
    """Query health_log by type(s) and date range.

    Returns list of dicts sorted by date ASC.
    If date is given, returns only that day's records (ignores days param).
    types=None returns all types.
    """
    conn = _connect()
    params: list = [user_id]
    sql = ("SELECT id, date, type, source, content, metrics, importance, "
           "created_at, updated_at FROM health_log WHERE user_id = ?")

    if date:
        sql += " AND date = ?"
        params.append(date)
    else:
        cutoff = (datetime.now(TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        sql += " AND date >= ?"
        params.append(cutoff)

    if types:
        type_ph = ",".join("?" * len(types))
        sql += f" AND type IN ({type_ph})"
        params.extend(types)

    sql += " ORDER BY date ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_health_log_items(item_ids: list[int]) -> int:
    """Hard delete health_log rows by id list. Returns count deleted."""
    if not item_ids:
        return 0
    conn = _connect()
    ph = ",".join("?" * len(item_ids))
    conn.execute(f"DELETE FROM health_log WHERE id IN ({ph})", item_ids)
    conn.commit()
    conn.close()
    return len(item_ids)
