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

        -- ──────────────────────────────────────────────────────────
        -- New tables (Phase 1 parity with private Mochi)
        -- ──────────────────────────────────────────────────────────

        -- Notes
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT    NOT NULL,
            content    TEXT    NOT NULL DEFAULT '',
            category   TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);

        -- Domain knowledge
        CREATE TABLE IF NOT EXISTS knowledge (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            domain     TEXT    NOT NULL,
            subject    TEXT    NOT NULL DEFAULT '',
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_domain ON knowledge(user_id, domain);

        -- Proactive message history
        CREATE TABLE IF NOT EXISTS proactive_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT    NOT NULL DEFAULT 'proactive',
            content    TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_created ON proactive_log(created_at);

        -- Operational context items (code digests, system metadata)
        CREATE TABLE IF NOT EXISTS ops_context_items (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            context_type  TEXT    NOT NULL DEFAULT '',
            content       TEXT    NOT NULL,
            source        TEXT    NOT NULL DEFAULT 'system',
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL,
            embedding     BLOB    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ops_context_user_type ON ops_context_items(user_id, context_type);

        -- Health time-series (oura, meals, symptoms, vitals)
        CREATE TABLE IF NOT EXISTS health_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            date       TEXT    NOT NULL,
            type       TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'oura_daily',
            content    TEXT    NOT NULL,
            metrics    TEXT    DEFAULT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hl_type_date ON health_log(user_id, type, date DESC);
        CREATE INDEX IF NOT EXISTS idx_hl_date ON health_log(user_id, date DESC);

        -- Pet device snapshots
        CREATE TABLE IF NOT EXISTS pet_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            date       TEXT    NOT NULL,
            pet_name   TEXT    DEFAULT NULL,
            source     TEXT    NOT NULL DEFAULT 'petkit_daily',
            content    TEXT    NOT NULL,
            metrics    TEXT    DEFAULT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pl_pet_date ON pet_log(user_id, pet_name, date DESC);

        -- Life events triage (events/mood/work)
        CREATE TABLE IF NOT EXISTS life_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            date       TEXT    NOT NULL,
            category   TEXT    NOT NULL,
            source     TEXT    NOT NULL DEFAULT 'memory_triage',
            content    TEXT    NOT NULL,
            importance INTEGER NOT NULL DEFAULT 1,
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ll_cat_date ON life_log(user_id, category, date DESC);

        -- Notification queue
        CREATE TABLE IF NOT EXISTS notifications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            ntype      TEXT    NOT NULL DEFAULT 'reminder',
            title      TEXT    NOT NULL DEFAULT '',
            body       TEXT    NOT NULL,
            channel_id INTEGER NOT NULL DEFAULT 0,
            acked      INTEGER NOT NULL DEFAULT 0,
            tg_sent    INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL,
            acked_at   TEXT    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications(acked, tg_sent, created_at);

        -- Soft-delete archive for memory items
        CREATE TABLE IF NOT EXISTS memory_trash (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id      INTEGER NOT NULL,
            user_id          INTEGER NOT NULL,
            category         TEXT    NOT NULL DEFAULT '',
            content          TEXT    NOT NULL,
            importance       INTEGER NOT NULL DEFAULT 1,
            source           TEXT    NOT NULL DEFAULT 'chat',
            deleted_by       TEXT    NOT NULL DEFAULT 'user',
            original_created TEXT    NOT NULL,
            deleted_at       TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_trash_deleted ON memory_trash(deleted_at);

        -- Per-skill admin configuration
        CREATE TABLE IF NOT EXISTS skill_config (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT    NOT NULL,
            key        TEXT    NOT NULL,
            value      TEXT    NOT NULL DEFAULT '',
            updated_at TEXT    NOT NULL,
            UNIQUE(skill_name, key)
        );

        -- Telegram sticker cache
        CREATE TABLE IF NOT EXISTS sticker_registry (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            file_id    TEXT    NOT NULL UNIQUE,
            set_name   TEXT    DEFAULT '',
            emoji      TEXT    DEFAULT '',
            tags       TEXT    DEFAULT '',
            created_at TEXT    NOT NULL
        );
    """)

    # ── Migrations (safe column additions for existing databases) ──────
    _run_migrations(conn)

    # ── FTS5 virtual table for memory full-text search ─────────────────
    _init_fts(conn)

    # ── sqlite-vec for native vector KNN (optional) ────────────────────
    _init_vec(conn)

    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


# Module-level flags for optional features
_FTS_AVAILABLE = False
_VEC_AVAILABLE = False


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Safe column additions for existing databases (ALTER TABLE + PRAGMA guard)."""

    def _has_col(table: str, col: str) -> bool:
        return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _add_col(table: str, col: str, typedef: str) -> None:
        if not _has_col(table, col):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            logger.info("Migrated %s: added %s", table, col)

    # messages
    _add_col("messages", "processed", "INTEGER NOT NULL DEFAULT 0")
    _add_col("messages", "image_data", "TEXT DEFAULT NULL")

    # memory_items
    _add_col("memory_items", "access_count", "INTEGER NOT NULL DEFAULT 0")
    _add_col("memory_items", "last_accessed", "TEXT NOT NULL DEFAULT ''")
    _add_col("memory_items", "embedding", "BLOB DEFAULT NULL")

    # reminders
    _add_col("reminders", "recurrence", "TEXT DEFAULT NULL")

    # todos
    _add_col("todos", "nudge_date", "TEXT DEFAULT NULL")

    # usage_log
    for col, typedef in [
        ("tool_name", "TEXT DEFAULT NULL"),
        ("model_role", "TEXT DEFAULT 'P'"),
        ("call_type", "TEXT DEFAULT 'chat'"),
        ("usage_stage", "TEXT DEFAULT ''"),
        ("prompt_system_tokens", "INTEGER DEFAULT NULL"),
        ("prompt_history_tokens", "INTEGER DEFAULT NULL"),
        ("prompt_tool_tokens", "INTEGER DEFAULT NULL"),
        ("cost_usd", "REAL DEFAULT NULL"),
    ]:
        _add_col("usage_log", col, typedef)

    # heartbeat_log: old schema (state/action/summary) → new (trigger/observations/actions/thought)
    if _has_col("heartbeat_log", "state") and not _has_col("heartbeat_log", "trigger"):
        conn.execute("ALTER TABLE heartbeat_log ADD COLUMN trigger TEXT NOT NULL DEFAULT 'delta'")
        conn.execute("ALTER TABLE heartbeat_log ADD COLUMN observations TEXT NOT NULL DEFAULT '{}'")
        conn.execute("ALTER TABLE heartbeat_log ADD COLUMN actions TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE heartbeat_log ADD COLUMN thought TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE heartbeat_log SET trigger = state, thought = summary")
        logger.info("Migrated heartbeat_log: state/action/summary → trigger/observations/actions/thought")

    # habits
    for col, typedef in [
        ("frequency", "TEXT NOT NULL DEFAULT 'daily'"),
        ("category", "TEXT NOT NULL DEFAULT ''"),
        ("downstream", "TEXT NOT NULL DEFAULT ''"),
        ("importance", "TEXT NOT NULL DEFAULT 'normal'"),
        ("context", "TEXT NOT NULL DEFAULT ''"),
        ("paused_until", "TEXT DEFAULT NULL"),
    ]:
        _add_col("habits", col, typedef)

    # habit_logs
    _add_col("habit_logs", "note", "TEXT NOT NULL DEFAULT ''")
    _add_col("habit_logs", "period", "TEXT NOT NULL DEFAULT ''")

    conn.commit()


def _init_fts(conn: sqlite3.Connection) -> None:
    """Initialize FTS5 virtual table for memory keyword search."""
    global _FTS_AVAILABLE
    fts_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_items_fts'"
    ).fetchone()
    if not fts_exists:
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE memory_items_fts USING fts5(
                    content, content_rowid='id', tokenize='unicode61'
                )
            """)
            # Backfill existing data
            rows = conn.execute("SELECT id, content FROM memory_items").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
                    (r["id"], r["content"]),
                )
            conn.commit()
            logger.info("Created memory_items_fts and backfilled %d rows", len(rows))
        except Exception as e:
            logger.warning("FTS5 init failed (not critical): %s", e)
    try:
        conn.execute("SELECT COUNT(*) FROM memory_items_fts")
        _FTS_AVAILABLE = True
    except Exception:
        _FTS_AVAILABLE = False


def _init_vec(conn: sqlite3.Connection) -> None:
    """Initialize sqlite-vec virtual table for native vector KNN (optional)."""
    global _VEC_AVAILABLE
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        vec_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_memories'"
        ).fetchone()
        if not vec_exists:
            from mochi.config import VEC_EMBEDDING_DIM
            conn.execute(
                f"CREATE VIRTUAL TABLE vec_memories USING vec0("
                f"item_id INTEGER PRIMARY KEY, "
                f"embedding float[{VEC_EMBEDDING_DIM}] distance_metric=cosine)"
            )
            count = 0
            for r in conn.execute(
                "SELECT id, embedding FROM memory_items WHERE embedding IS NOT NULL"
            ).fetchall():
                conn.execute(
                    "INSERT INTO vec_memories(item_id, embedding) VALUES (?, ?)",
                    (r["id"], r["embedding"]),
                )
                count += 1
            conn.commit()
            if count:
                logger.info("Created vec_memories and backfilled %d rows", count)

        _VEC_AVAILABLE = True
        logger.info("sqlite-vec loaded, native vector search enabled")
    except ImportError:
        logger.info("sqlite-vec not installed (pip install sqlite-vec for native vector search)")
        _VEC_AVAILABLE = False
    except Exception as e:
        logger.warning("sqlite-vec init failed: %s", e)
        _VEC_AVAILABLE = False


def fts_upsert(item_id: int, content: str) -> None:
    """Update FTS index for a memory item."""
    if not _FTS_AVAILABLE:
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM memory_items_fts WHERE rowid = ?", (item_id,))
        conn.execute(
            "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
            (item_id, content),
        )
        conn.commit()
    except Exception as e:
        logger.warning("FTS upsert failed for item %d: %s", item_id, e)
    finally:
        conn.close()


def fts_delete(item_ids: list[int]) -> None:
    """Remove items from FTS index."""
    if not _FTS_AVAILABLE or not item_ids:
        return
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(f"DELETE FROM memory_items_fts WHERE rowid IN ({placeholders})", item_ids)
        conn.commit()
    except Exception as e:
        logger.warning("FTS delete failed: %s", e)
    finally:
        conn.close()


def vec_upsert(item_id: int, embedding: bytes) -> None:
    """Update vector index for a memory item."""
    if not _VEC_AVAILABLE or not embedding:
        return
    conn = _connect()
    try:
        conn.execute("DELETE FROM vec_memories WHERE item_id = ?", (item_id,))
        conn.execute(
            "INSERT INTO vec_memories(item_id, embedding) VALUES (?, ?)",
            (item_id, embedding),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Vec upsert failed for item %d: %s", item_id, e)
    finally:
        conn.close()


def vec_delete(item_ids: list[int]) -> None:
    """Remove items from vector index."""
    if not _VEC_AVAILABLE or not item_ids:
        return
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(f"DELETE FROM vec_memories WHERE item_id IN ({placeholders})", item_ids)
        conn.commit()
    except Exception as e:
        logger.warning("Vec delete failed: %s", e)
    finally:
        conn.close()

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
              tool_calls: int = 0, model: str = "", purpose: str = "chat",
              tool_name: str | None = None, model_role: str = "P",
              call_type: str | None = None, usage_stage: str = "",
              prompt_system_tokens: int | None = None,
              prompt_history_tokens: int | None = None,
              prompt_tool_tokens: int | None = None,
              cost_usd: float | None = None) -> None:
    now = datetime.now(TZ).isoformat()
    eff_call_type = call_type or purpose
    conn = _connect()
    conn.execute(
        """INSERT INTO usage_log (prompt_tokens, completion_tokens, total_tokens,
           tool_calls, model, purpose, created_at,
           tool_name, model_role, call_type, usage_stage,
           prompt_system_tokens, prompt_history_tokens, prompt_tool_tokens, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (prompt_tokens, completion_tokens, total_tokens, tool_calls, model, purpose, now,
         tool_name, model_role, eff_call_type, usage_stage,
         prompt_system_tokens, prompt_history_tokens, prompt_tool_tokens, cost_usd),
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


# ═══════════════════════════════════════════════════════════════════════════
# Skill Config
# ═══════════════════════════════════════════════════════════════════════════

def get_disabled_skills() -> set[str]:
    """Return set of skill names that are admin-disabled."""
    conn = _connect()
    rows = conn.execute(
        "SELECT skill_name FROM skill_config WHERE key = '_enabled' AND value = 'false'"
    ).fetchall()
    conn.close()
    return {r["skill_name"] for r in rows}


def set_skill_enabled(skill_name: str, enabled: bool) -> None:
    """Enable or disable a skill via admin config."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    if enabled:
        conn.execute(
            "DELETE FROM skill_config WHERE skill_name = ? AND key = '_enabled'",
            (skill_name,),
        )
    else:
        conn.execute(
            "INSERT INTO skill_config (skill_name, key, value, updated_at) "
            "VALUES (?, '_enabled', 'false', ?) "
            "ON CONFLICT(skill_name, key) DO UPDATE SET value = 'false', updated_at = ?",
            (skill_name, now, now),
        )
    conn.commit()
    conn.close()
