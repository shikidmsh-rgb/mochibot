"""SQLite database layer — persistent storage for messages, memory, reminders, todos.

Lightweight schema. Tables are created automatically on first run.
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mochi.config import DB_PATH, TIMEZONE_OFFSET_HOURS, HEARTBEAT_LOG_DELETE_DAYS, HEARTBEAT_LOG_TRIM_DAYS

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


def _connect() -> sqlite3.Connection:
    """Return a connection with row_factory set."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    conn = _connect()
    conn.executescript("""
        -- Chat messages
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_user
            ON messages(user_id, created_at);

        -- Layer 2: Memory items (extracted facts, preferences, events)
        CREATE TABLE IF NOT EXISTS memory_items (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            category   TEXT    NOT NULL DEFAULT '',
            content    TEXT    NOT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            source     TEXT    NOT NULL DEFAULT 'extracted',
            processed  INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_items_user
            ON memory_items(user_id, category);

        -- Layer 1: Core memory (compact summary, rebuilt nightly)
        CREATE TABLE IF NOT EXISTS core_memory (
            user_id    INTEGER PRIMARY KEY,
            content    TEXT    NOT NULL DEFAULT '',
            updated_at TEXT    NOT NULL
        );

        -- Reminders
        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            channel_id INTEGER NOT NULL DEFAULT 0,
            message    TEXT    NOT NULL,
            remind_at  TEXT    NOT NULL,
            fired      INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_reminders_pending
            ON reminders(fired, remind_at);

        -- Todos
        CREATE TABLE IF NOT EXISTS todos (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            task       TEXT    NOT NULL,
            done       INTEGER NOT NULL DEFAULT 0,
            category   TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_todos_user
            ON todos(user_id, done);

        -- LLM usage tracking
        CREATE TABLE IF NOT EXISTS usage_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens      INTEGER NOT NULL DEFAULT 0,
            tool_calls        INTEGER NOT NULL DEFAULT 0,
            model             TEXT    NOT NULL DEFAULT '',
            purpose           TEXT    NOT NULL DEFAULT 'chat',
            created_at        TEXT    NOT NULL
        );

        -- Heartbeat logs
        CREATE TABLE IF NOT EXISTS heartbeat_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            state      TEXT    NOT NULL,
            action     TEXT    NOT NULL DEFAULT 'none',
            summary    TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_heartbeat_created
            ON heartbeat_log(created_at);

        -- Skill run history
        CREATE TABLE IF NOT EXISTS skill_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT    NOT NULL,
            trigger    TEXT    NOT NULL DEFAULT 'tool_call',
            success    INTEGER NOT NULL DEFAULT 1,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            summary    TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );

        -- Habits (defined by user)
        CREATE TABLE IF NOT EXISTS habits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            description TEXT    NOT NULL DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_habits_user_name
            ON habits(user_id, name);

        -- Habit log entries (each time user marks a habit done)
        CREATE TABLE IF NOT EXISTS habit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id   INTEGER NOT NULL REFERENCES habits(id),
            user_id    INTEGER NOT NULL,
            logged_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_habit_logs_habit
            ON habit_logs(habit_id, logged_at);
    """)
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# Messages
# ═══════════════════════════════════════════════════════════════════════════

def save_message(user_id: int, role: str, content: str) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (user_id, role, content, now),
    )
    conn.commit()
    conn.close()


def get_recent_messages(user_id: int, limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def get_unprocessed_conversations(user_id: int) -> list[dict]:
    """Get messages not yet processed for memory extraction."""
    conn = _connect()
    rows = conn.execute(
        """SELECT id, role, content, created_at FROM messages
           WHERE user_id = ? AND id > COALESCE(
               (SELECT MAX(id) FROM messages WHERE user_id = ? AND content LIKE '%[memory_extracted]%'), 0
           )
           ORDER BY id""",
        (user_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_messages_processed(user_id: int, up_to_id: int) -> None:
    """Mark messages as processed for memory extraction."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, 'system', ?, ?)",
        (user_id, f"[memory_extracted] up_to_id={up_to_id}", now),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Memory Items (Layer 2)
# ═══════════════════════════════════════════════════════════════════════════

def save_memory_item(user_id: int, category: str, content: str,
                     importance: int = 1, source: str = "extracted") -> int:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO memory_items (user_id, category, content, importance, source, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, category, content, importance, source, now, now),
    )
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


def recall_memory(user_id: int, query: str = "", category: str = "",
                  limit: int = 20) -> list[dict]:
    conn = _connect()
    sql = "SELECT id, category, content, importance, created_at FROM memory_items WHERE user_id = ?"
    params: list = [user_id]

    if category:
        sql += " AND category = ?"
        params.append(category)
    if query:
        sql += " AND content LIKE ?"
        params.append(f"%{query}%")

    sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_memory_items(user_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, category, content, importance, source, created_at FROM memory_items WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_memory_items(ids: list[int]) -> int:
    if not ids:
        return 0
    conn = _connect()
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM memory_items WHERE id IN ({placeholders})", ids)
    conn.commit()
    count = cur.rowcount
    conn.close()
    return count


def merge_memory_items(keep_id: int, delete_ids: list[int], merged_content: str) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET content = ?, updated_at = ? WHERE id = ?",
        (merged_content, now, keep_id),
    )
    if delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        conn.execute(f"DELETE FROM memory_items WHERE id IN ({placeholders})", delete_ids)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Core Memory (Layer 1)
# ═══════════════════════════════════════════════════════════════════════════

def get_core_memory(user_id: int) -> str:
    conn = _connect()
    row = conn.execute(
        "SELECT content FROM core_memory WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["content"] if row else ""


def update_core_memory(user_id: int, content: str) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        """INSERT INTO core_memory (user_id, content, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at""",
        (user_id, content, now),
    )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Reminders
# ═══════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════
# Todos
# ═══════════════════════════════════════════════════════════════════════════

def create_todo(user_id: int, task: str, category: str = "") -> int:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO todos (user_id, task, category, created_at) VALUES (?, ?, ?, ?)",
        (user_id, task, category, now),
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def get_todos(user_id: int, include_done: bool = False) -> list[dict]:
    conn = _connect()
    sql = "SELECT id, task, done, category, created_at FROM todos WHERE user_id = ?"
    params: list = [user_id]
    if not include_done:
        sql += " AND done = 0"
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def complete_todo(todo_id: int) -> None:
    conn = _connect()
    conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (todo_id,))
    conn.commit()
    conn.close()


def delete_todo(todo_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Usage Logging
# ═══════════════════════════════════════════════════════════════════════════

def log_usage(prompt_tokens: int, completion_tokens: int, total_tokens: int,
              tool_calls: int = 0, model: str = "", purpose: str = "chat") -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        """INSERT INTO usage_log (prompt_tokens, completion_tokens, total_tokens,
           tool_calls, model, purpose, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (prompt_tokens, completion_tokens, total_tokens, tool_calls, model, purpose, now),
    )
    conn.commit()
    conn.close()


def get_usage_summary(days: int = 30) -> dict:
    """Return usage summary for /cost command.

    Returns:
        {
            "today": {"prompt": int, "completion": int, "total": int, "calls": int},
            "month": {"prompt": int, "completion": int, "total": int, "calls": int},
            "by_model": {"gpt-4o": {"total": int, "calls": int}, ...},
            "by_purpose": {"chat": int, "think": int, ...},
        }
    """
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    conn = _connect()

    # Today
    row = conn.execute(
        """SELECT COALESCE(SUM(prompt_tokens), 0) as p,
                  COALESCE(SUM(completion_tokens), 0) as c,
                  COALESCE(SUM(total_tokens), 0) as t,
                  COUNT(*) as n
           FROM usage_log WHERE created_at >= ?""",
        (today_start,),
    ).fetchone()
    today = {"prompt": row["p"], "completion": row["c"], "total": row["t"], "calls": row["n"]}

    # This month
    row = conn.execute(
        """SELECT COALESCE(SUM(prompt_tokens), 0) as p,
                  COALESCE(SUM(completion_tokens), 0) as c,
                  COALESCE(SUM(total_tokens), 0) as t,
                  COUNT(*) as n
           FROM usage_log WHERE created_at >= ?""",
        (month_start,),
    ).fetchone()
    month = {"prompt": row["p"], "completion": row["c"], "total": row["t"], "calls": row["n"]}

    # By model (this month)
    by_model = {}
    for r in conn.execute(
        """SELECT model, COALESCE(SUM(total_tokens), 0) as t, COUNT(*) as n
           FROM usage_log WHERE created_at >= ? GROUP BY model""",
        (month_start,),
    ).fetchall():
        by_model[r["model"] or "unknown"] = {"total": r["t"], "calls": r["n"]}

    # By purpose (this month)
    by_purpose = {}
    for r in conn.execute(
        """SELECT purpose, COALESCE(SUM(total_tokens), 0) as t
           FROM usage_log WHERE created_at >= ? GROUP BY purpose""",
        (month_start,),
    ).fetchall():
        by_purpose[r["purpose"] or "other"] = r["t"]

    conn.close()
    return {"today": today, "month": month, "by_model": by_model, "by_purpose": by_purpose}


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat Logs
# ═══════════════════════════════════════════════════════════════════════════

def log_heartbeat(state: str, action: str = "none", summary: str = "") -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO heartbeat_log (state, action, summary, created_at) VALUES (?, ?, ?, ?)",
        (state, action, summary, now),
    )
    conn.commit()
    conn.close()


def get_last_heartbeat_log() -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM heartbeat_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ═══════════════════════════════════════════════════════════════════════════
# Skill Runs
# ═══════════════════════════════════════════════════════════════════════════

def log_skill_run(skill_name: str, trigger: str, success: bool,
                  duration_ms: int = 0, summary: str = "") -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        """INSERT INTO skill_runs (skill_name, trigger, success, duration_ms, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (skill_name, trigger, int(success), duration_ms, summary, now),
    )
    conn.commit()
    conn.close()


def get_last_user_message_time(user_id: int) -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT created_at FROM messages WHERE user_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["created_at"] if row else None


def get_message_count_today(user_id: int) -> int:
    """Count user messages sent today (for conversation pattern observation)."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ? AND role = 'user' AND created_at >= ?",
        (user_id, today),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_daily_message_counts(user_id: int, days: int = 7) -> list[dict]:
    """Get per-day user message counts for the last N days.

    Returns: [{"date": "2026-02-22", "count": 15}, ...] ordered oldest→newest.
    Always returns exactly `days` entries (count=0 for silent days).
    """
    now = datetime.now(TZ)
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = _connect()
    rows = conn.execute(
        "SELECT DATE(created_at) as day, COUNT(*) as cnt "
        "FROM messages WHERE user_id = ? AND role = 'user' AND created_at >= ? "
        "GROUP BY DATE(created_at) ORDER BY day",
        (user_id, start),
    ).fetchall()
    conn.close()

    # Build full date range with 0-fills
    counts_map = {r["day"]: r["cnt"] for r in rows}
    result = []
    for i in range(days):
        d = (now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        result.append({"date": d, "count": counts_map.get(d, 0)})
    return result


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


def get_active_todo_count(user_id: int) -> int:
    """Count active (not done) todos for a user."""
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM todos WHERE user_id = ? AND done = 0",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ═══════════════════════════════════════════════════════════════════════════
# Habits
# ═══════════════════════════════════════════════════════════════════════════

def create_habit(user_id: int, name: str, description: str = "") -> int:
    """Create a new habit. Returns habit id."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT OR IGNORE INTO habits (user_id, name, description, created_at) VALUES (?, ?, ?, ?)",
        (user_id, name, description, now),
    )
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def log_habit(user_id: int, habit_name: str) -> bool:
    """Log a habit completion for today. Returns True on success."""
    conn = _connect()
    row = conn.execute(
        "SELECT id FROM habits WHERE user_id = ? AND name = ? AND active = 1",
        (user_id, habit_name),
    ).fetchone()
    if not row:
        conn.close()
        return False
    habit_id = row["id"]
    now = datetime.now(TZ).isoformat()
    conn.execute(
        "INSERT INTO habit_logs (habit_id, user_id, logged_at) VALUES (?, ?, ?)",
        (habit_id, user_id, now),
    )
    conn.commit()
    conn.close()
    return True


def get_habits_overview(user_id: int) -> list[dict]:
    """Return active habits with streak and last-logged info.

    Each item: {name, description, streak_days, last_logged, logged_today}
    """
    now = datetime.now(TZ)
    today_str = now.strftime("%Y-%m-%d")

    conn = _connect()
    habits = conn.execute(
        "SELECT id, name, description FROM habits WHERE user_id = ? AND active = 1 ORDER BY name",
        (user_id,),
    ).fetchall()

    result = []
    for h in habits:
        habit_id = h["id"]

        # Last log date
        last_row = conn.execute(
            "SELECT logged_at FROM habit_logs WHERE habit_id = ? ORDER BY logged_at DESC LIMIT 1",
            (habit_id,),
        ).fetchone()
        last_logged = last_row["logged_at"][:10] if last_row else None

        # Logged today?
        logged_today = last_logged == today_str

        # Streak: count consecutive days ending today (or yesterday)
        streak = _compute_streak(conn, habit_id, today_str)

        result.append({
            "name": h["name"],
            "description": h["description"],
            "streak_days": streak,
            "last_logged": last_logged,
            "logged_today": logged_today,
        })

    conn.close()
    return result


def _compute_streak(conn: sqlite3.Connection, habit_id: int, today_str: str) -> int:
    """Count consecutive days the habit was logged, ending on today or yesterday."""
    from datetime import date

    today = date.fromisoformat(today_str)
    # Collect distinct logged dates
    rows = conn.execute(
        "SELECT DISTINCT DATE(logged_at) as d FROM habit_logs WHERE habit_id = ? ORDER BY d DESC",
        (habit_id,),
    ).fetchall()
    logged_dates = {date.fromisoformat(r["d"]) for r in rows}

    streak = 0
    check = today
    # Allow today OR yesterday as starting point
    if check not in logged_dates:
        check = today - timedelta(days=1)
    while check in logged_dates:
        streak += 1
        check -= timedelta(days=1)
    return streak
