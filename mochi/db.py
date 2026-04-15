"""SQLite database layer — persistent storage for messages, memory, reminders, todos.

Lightweight schema. Tables are created automatically on first run.
"""

import difflib
import json
import math
import re
import struct
import sqlite3
import logging
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mochi.config import (
    DB_PATH, TIMEZONE_OFFSET_HOURS, HEARTBEAT_LOG_DELETE_DAYS, HEARTBEAT_LOG_TRIM_DAYS, TZ,
    RECALL_VEC_SIM_THRESHOLD, RECALL_BM25_WEIGHT, RECALL_VEC_SIM_WEIGHT,
    RECALL_KEYWORD_BOOST, RECALL_FTS_CANDIDATE_MULTIPLIER, RECALL_FALLBACK_LIMIT,
    RECALL_DECAY_HALF_LIFE_DAYS, VEC_SEARCH_NATIVE_ENABLED, VEC_SEARCH_CANDIDATE_LIMIT,
)

logger = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    """Return a connection with row_factory set."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, typedef: str) -> bool:
    """Add *column* to *table* if it does not already exist.

    Safe to call repeatedly (idempotent).  Intended for use inside
    ``Skill.init_schema()`` for lightweight schema migrations.

    Returns True if the column was added, False if it already existed.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
        logger.info("Migrated %s: added %s", table, column)
        return True
    return False


def init_db() -> None:
    """Create framework-level tables if they don't exist.

    Skill-specific tables are created by each skill's ``init_schema()``
    method, called separately via ``init_all_skill_schemas()`` after
    ``discover()``.
    """
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

        -- Domain knowledge (reserved)
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

        -- Pet device snapshots (reserved)
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

        -- Life events triage (reserved)
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

        -- Notes (reserved — NoteSkill uses file-based storage)
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT    NOT NULL,
            content    TEXT    NOT NULL DEFAULT '',
            category   TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);

        -- Notification queue (reserved)
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

        -- Model registry (admin portal)
        CREATE TABLE IF NOT EXISTS model_registry (
            name       TEXT PRIMARY KEY,
            provider   TEXT NOT NULL,
            model      TEXT NOT NULL,
            api_key    TEXT NOT NULL DEFAULT '',
            base_url   TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        -- Tier-to-model assignments (admin portal)
        CREATE TABLE IF NOT EXISTS tier_assignments (
            tier       TEXT PRIMARY KEY,
            model_name TEXT NOT NULL REFERENCES model_registry(name) ON DELETE CASCADE,
            updated_at TEXT NOT NULL
        );

        -- Knowledge Graph (entity-relationship triples — framework-level, not skill-owned)
        CREATE TABLE IF NOT EXISTS kg_entities (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            name         TEXT    NOT NULL,
            display_name TEXT    NOT NULL,
            entity_type  TEXT    NOT NULL DEFAULT 'concept',
            created_at   TEXT    NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_entity_user_name
            ON kg_entities(user_id, name);

        CREATE TABLE IF NOT EXISTS kg_triples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            subject_id  INTEGER NOT NULL REFERENCES kg_entities(id),
            predicate   TEXT    NOT NULL,
            object_id   INTEGER NOT NULL REFERENCES kg_entities(id),
            valid_from  TEXT    DEFAULT NULL,
            valid_to    TEXT    DEFAULT NULL,
            source      TEXT    NOT NULL DEFAULT 'chat',
            confidence  REAL    NOT NULL DEFAULT 1.0,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kg_triple_subject
            ON kg_triples(subject_id, valid_to);
        CREATE INDEX IF NOT EXISTS idx_kg_triple_user
            ON kg_triples(user_id, valid_to);
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
    """Safe column additions for framework-level tables.

    Skill-specific migrations live in each skill's ``init_schema()`` method.
    """

    def _has_col(table: str, col: str) -> bool:
        return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

    def _add_col(table: str, col: str, typedef: str) -> None:
        if not _has_col(table, col):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            logger.info("Migrated %s: added %s", table, col)

    # messages
    _add_col("messages", "processed", "INTEGER NOT NULL DEFAULT 0")
    _add_col("messages", "image_data", "TEXT DEFAULT NULL")
    _add_col("messages", "tool_history", "TEXT DEFAULT NULL")

    # memory_items
    _add_col("memory_items", "access_count", "INTEGER NOT NULL DEFAULT 0")
    _add_col("memory_items", "last_accessed", "TEXT NOT NULL DEFAULT ''")
    _add_col("memory_items", "embedding", "BLOB DEFAULT NULL")

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
            # Backfill with pre-tokenized content for CJK support
            rows = conn.execute("SELECT id, content FROM memory_items").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
                    (r["id"], _fts_tokenize(r["content"])),
                )
            conn.commit()
            logger.info("Created memory_items_fts and backfilled %d rows", len(rows))
        except Exception as e:
            logger.warning("FTS5 init failed (not critical): %s", e)
    else:
        # One-time re-index: migrate from raw unicode61 to pre-tokenized CJK bigrams.
        # Check sentinel in skill_config table; if not set, re-index all rows.
        try:
            sentinel = conn.execute(
                "SELECT value FROM skill_config WHERE skill_name='_system' AND key='fts_tokenized'",
            ).fetchone()
            if not sentinel:
                rows = conn.execute("SELECT id, content FROM memory_items").fetchall()
                if rows:
                    conn.execute("DELETE FROM memory_items_fts")
                    for r in rows:
                        conn.execute(
                            "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
                            (r["id"], _fts_tokenize(r["content"])),
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO skill_config(skill_name, key, value) VALUES ('_system', 'fts_tokenized', '1')",
                    )
                    conn.commit()
                    logger.info("Re-indexed %d FTS rows with CJK tokenization", len(rows))
        except Exception as e:
            logger.debug("FTS re-index check failed: %s", e)
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


# ── Helpers for hybrid search ────────────────────────────────────────────


def _fts_tokenize(text: str) -> str:
    """Pre-tokenize text for FTS5: overlapping bigrams for CJK, words for English."""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    tokens: list[str] = []
    alpha_buf: list[str] = []
    cjk_buf: list[str] = []

    def _is_cjk(ch: str) -> bool:
        cp = ord(ch)
        return 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF

    def flush_alpha():
        if alpha_buf:
            word = "".join(alpha_buf).strip()
            if word:
                tokens.append(word)
            alpha_buf.clear()

    def flush_cjk():
        if len(cjk_buf) == 1:
            tokens.append(cjk_buf[0])
        elif len(cjk_buf) >= 2:
            for i in range(len(cjk_buf) - 1):
                tokens.append(cjk_buf[i] + cjk_buf[i + 1])
        cjk_buf.clear()

    for ch in normalized:
        if _is_cjk(ch):
            flush_alpha()
            cjk_buf.append(ch)
        elif ch.isalnum():
            flush_cjk()
            alpha_buf.append(ch)
        else:
            flush_cjk()
            flush_alpha()

    flush_cjk()
    flush_alpha()
    return " ".join(tokens)


def _cosine_similarity(a: bytes, b: bytes) -> float:
    """Compute cosine similarity between two packed float32 embedding blobs."""
    if not a or not b or len(a) != len(b):
        return 0.0
    n = len(a) // 4
    va = struct.unpack(f"{n}f", a)
    vb = struct.unpack(f"{n}f", b)
    dot = sum(x * y for x, y in zip(va, vb))
    norm_a = sum(x * x for x in va) ** 0.5
    norm_b = sum(x * x for x in vb) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _load_vec_conn(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension on a given connection. Returns True on success."""
    if not _VEC_AVAILABLE:
        return False
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def fts_upsert(item_id: int, content: str,
               conn: sqlite3.Connection | None = None) -> None:
    """Update FTS index for a memory item (pre-tokenized for CJK support).

    If *conn* is provided, use it (caller owns commit/close).
    Otherwise open+commit+close a fresh connection.
    """
    if not _FTS_AVAILABLE:
        return
    tokenized = _fts_tokenize(content)
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        conn.execute("DELETE FROM memory_items_fts WHERE rowid = ?", (item_id,))
        conn.execute(
            "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
            (item_id, tokenized),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.warning("FTS upsert failed for item %d: %s", item_id, e)
    finally:
        if own_conn:
            conn.close()


def fts_delete(item_ids: list[int],
               conn: sqlite3.Connection | None = None) -> None:
    """Remove items from FTS index.

    If *conn* is provided, use it (caller owns commit/close).
    """
    if not _FTS_AVAILABLE or not item_ids:
        return
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(f"DELETE FROM memory_items_fts WHERE rowid IN ({placeholders})", item_ids)
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.warning("FTS delete failed: %s", e)
    finally:
        if own_conn:
            conn.close()


def vec_upsert(item_id: int, embedding: bytes,
               conn: sqlite3.Connection | None = None) -> None:
    """Update vector index for a memory item.

    If *conn* is provided, use it (caller owns commit/close).
    """
    if not _VEC_AVAILABLE or not embedding:
        return
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    if not _load_vec_conn(conn):
        if own_conn:
            conn.close()
        return
    try:
        conn.execute("DELETE FROM vec_memories WHERE item_id = ?", (item_id,))
        conn.execute(
            "INSERT INTO vec_memories(item_id, embedding) VALUES (?, ?)",
            (item_id, embedding),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.warning("Vec upsert failed for item %d: %s", item_id, e)
    finally:
        if own_conn:
            conn.close()


def vec_delete(item_ids: list[int],
               conn: sqlite3.Connection | None = None) -> None:
    """Remove items from vector index.

    If *conn* is provided, use it (caller owns commit/close).
    """
    if not _VEC_AVAILABLE or not item_ids:
        return
    own_conn = conn is None
    if own_conn:
        conn = _connect()
    if not _load_vec_conn(conn):
        if own_conn:
            conn.close()
        return
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(f"DELETE FROM vec_memories WHERE item_id IN ({placeholders})", item_ids)
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.warning("Vec delete failed: %s", e)
    finally:
        if own_conn:
            conn.close()

def save_message(user_id: int, role: str, content: str, tool_history: str | None = None) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at, tool_history) VALUES (?, ?, ?, ?, ?)",
        (user_id, role, content, now, tool_history),
    )
    conn.commit()
    conn.close()


def get_recent_messages(user_id: int, limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT role, content, created_at, tool_history FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
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
                     importance: int = 1, source: str = "extracted",
                     embedding: bytes | None = None,
                     append: bool = False,
                     match_hint: str | None = None) -> int:
    """Save a memory item with on-insert smart dedup.

    Dedup priority:
      1. match_hint keyword search (action=update from LLM)
      2. Date-keyed prefix match ([YYYY-MM-DD]...)
      3. Text similarity (normalized, SequenceMatcher)
      4. Vector cosine similarity (if embedding provided)
    If a match is found: UPDATE (keep longer content, bump importance/access).
    Otherwise: INSERT new row.

    append: if True and dated match found, concatenate new content with ' | '.
    match_hint: keyword to locate old memory to overwrite (status updates).
    """
    now = datetime.now(TZ).isoformat()
    conn = _connect()

    def _normalize_text(text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text or "").lower()
        return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")

    def _extract_date(text: str) -> str | None:
        m = re.search(r"\d{4}-\d{2}-\d{2}", text or "")
        return m.group(0) if m else None

    # --- Priority 1: match_hint (action=update) ---
    hint_matched = None
    if match_hint:
        hint_rows = conn.execute(
            "SELECT id, content, access_count, embedding FROM memory_items "
            "WHERE user_id = ? AND category = ? AND content LIKE ? "
            "ORDER BY updated_at DESC LIMIT 10",
            (user_id, category, f"%{match_hint}%"),
        ).fetchall()
        if hint_rows:
            hint_matched = hint_rows[0]

    # --- Priority 2-4: standard dedup ---
    date_match = re.match(r"^\[\d{4}-\d{2}-\d{2}\]", content)
    content_date = _extract_date(content)
    norm_content = _normalize_text(content)
    existing = None

    # P2: Date-keyed prefix match
    if date_match:
        existing = conn.execute(
            "SELECT id, content, access_count, embedding FROM memory_items "
            "WHERE user_id = ? AND category = ? AND content LIKE ? LIMIT 1",
            (user_id, category, f"{date_match.group(0)}%"),
        ).fetchone()
    else:
        # Quick prefix check
        existing = conn.execute(
            "SELECT id, content, access_count, embedding FROM memory_items "
            "WHERE user_id = ? AND category = ? AND content LIKE ? LIMIT 1",
            (user_id, category, f"{content[:20]}%"),
        ).fetchone()

        # P3: Text similarity scan
        if not existing:
            candidates = conn.execute(
                "SELECT id, content, access_count, embedding FROM memory_items "
                "WHERE user_id = ? AND category = ? "
                "AND content NOT LIKE '[____-__-__]%' "
                "ORDER BY updated_at DESC LIMIT 120",
                (user_id, category),
            ).fetchall()

            for cand in candidates:
                norm_cand = _normalize_text(cand["content"])
                if not norm_content or not norm_cand:
                    continue
                if norm_content == norm_cand:
                    existing = cand
                    break
                ratio = difflib.SequenceMatcher(None, norm_content, norm_cand).ratio()
                cand_date = _extract_date(cand["content"])
                same_day = bool(content_date and cand_date and content_date == cand_date)
                if (same_day and ratio >= 0.74) or ratio >= 0.92:
                    existing = cand
                    break

            # P4: Vector similarity
            if not existing and embedding:
                for cand in candidates:
                    cand_emb = cand["embedding"] if "embedding" in cand.keys() else None
                    if not cand_emb:
                        continue
                    if _cosine_similarity(embedding, cand_emb) >= 0.92:
                        existing = cand
                        break

    # hint_matched takes priority if no standard dedup hit
    if hint_matched and not existing:
        existing = hint_matched

    if existing:
        # Skip if content is identical
        if existing["content"] == content:
            conn.close()
            return existing["id"]

        # Decide what to keep
        if hint_matched and existing["id"] == hint_matched["id"]:
            keep_content = content
            keep_emb = embedding
        elif append and date_match:
            new_body = content[len(date_match.group(0)):].strip()
            old_body = existing["content"]
            if new_body and new_body not in old_body:
                keep_content = f"{old_body} | {new_body}"
                keep_emb = None
            else:
                conn.close()
                return existing["id"]
        else:
            keep_content = content if len(content) >= len(existing["content"]) else existing["content"]
            keep_emb = embedding if len(content) >= len(existing["content"]) else (
                existing["embedding"] if "embedding" in existing.keys() else None
            )

        if keep_emb is not None:
            conn.execute(
                "UPDATE memory_items SET content = ?, importance = MAX(importance, ?), "
                "updated_at = ?, access_count = access_count + 1, embedding = ? WHERE id = ?",
                (keep_content, importance, now, keep_emb, existing["id"]),
            )
        else:
            conn.execute(
                "UPDATE memory_items SET content = ?, importance = MAX(importance, ?), "
                "updated_at = ?, access_count = access_count + 1 WHERE id = ?",
                (keep_content, importance, now, existing["id"]),
            )
        item_id = existing["id"]
        # Update FTS + vec indices (same conn — not yet committed)
        fts_upsert(item_id, keep_content, conn)
        if keep_emb is not None:
            vec_upsert(item_id, keep_emb, conn)
    else:
        cur = conn.execute(
            "INSERT INTO memory_items (user_id, category, content, importance, "
            "source, created_at, updated_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, category, content, importance, source, now, now, embedding),
        )
        item_id = cur.lastrowid
        fts_upsert(item_id, content, conn)
        if embedding:
            vec_upsert(item_id, embedding, conn)

    conn.commit()
    conn.close()
    return item_id


def recall_memory(user_id: int, query: str = "", category: str = "",
                  limit: int = 20,
                  exclude_categories: list[str] | None = None,
                  query_embedding: bytes | None = None,
                  bump_access: bool = True) -> list[dict]:
    """Recall memories — hybrid FTS5 BM25 + vector search with decay scoring.

    Pipeline:
      1a. sqlite-vec MATCH → top-K nearest neighbours + distances
      1b. FTS5 MATCH → candidate IDs + BM25 scores
      2.  Fallback expansion if too few candidates
      3.  Fetch full rows for candidates only
      4.  Hybrid scoring: vec_sim + bm25 + importance + decay
    """
    conn = _connect()
    vec_ok = _load_vec_conn(conn) if query_embedding else False
    now = datetime.now(TZ)
    exclude = set(exclude_categories or [])

    # Candidate accumulators: item_id → score component
    vec_scores: dict[int, float] = {}   # item_id → cosine distance
    bm25_scores: dict[int, float] = {}  # item_id → raw BM25 rank

    # ── Phase 1a: Vector KNN ─────────────────────────────────────────
    if vec_ok and query_embedding and VEC_SEARCH_NATIVE_ENABLED:
        try:
            k = VEC_SEARCH_CANDIDATE_LIMIT
            vec_rows = conn.execute(
                "SELECT item_id, distance FROM vec_memories "
                "WHERE embedding MATCH ? AND k = ?",
                (query_embedding, k),
            ).fetchall()
            for r in vec_rows:
                vec_scores[r["item_id"]] = r["distance"]
        except Exception as e:
            logger.debug("Vec KNN search failed: %s", e)

    # ── Phase 1b: FTS5 BM25 ─────────────────────────────────────────
    if _FTS_AVAILABLE and query:
        try:
            fts_query = _fts_tokenize(query)
            if fts_query.strip():
                fts_limit = limit * RECALL_FTS_CANDIDATE_MULTIPLIER
                fts_rows = conn.execute(
                    "SELECT fts.rowid AS id, fts.rank AS bm25_raw "
                    "FROM memory_items_fts fts "
                    "JOIN memory_items m ON m.id = fts.rowid "
                    "WHERE fts.content MATCH ? AND m.user_id = ? "
                    "ORDER BY fts.rank LIMIT ?",
                    (fts_query, user_id, fts_limit),
                ).fetchall()
                for r in fts_rows:
                    bm25_scores[r["id"]] = r["bm25_raw"]
        except Exception as e:
            logger.debug("FTS5 search failed: %s", e)

    # ── Phase 2: Fallback expansion ──────────────────────────────────
    candidate_ids = set(vec_scores.keys()) | set(bm25_scores.keys())
    if len(candidate_ids) < limit:
        fallback_limit = RECALL_FALLBACK_LIMIT
        # Build WHERE clause
        conditions = ["user_id = ?"]
        params: list = [user_id]
        if category:
            conditions.append("category = ?")
            params.append(category)
        if exclude:
            ph = ",".join("?" * len(exclude))
            conditions.append(f"category NOT IN ({ph})")
            params.extend(exclude)
        if query and not bm25_scores:
            # No FTS available — fall back to LIKE
            conditions.append("content LIKE ?")
            params.append(f"%{query}%")
        where = " AND ".join(conditions)
        params.append(fallback_limit)
        fb_rows = conn.execute(
            f"SELECT id FROM memory_items WHERE {where} "
            f"ORDER BY importance DESC, updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        for r in fb_rows:
            candidate_ids.add(r["id"])

    if not candidate_ids:
        conn.close()
        return []

    # ── Phase 3: Fetch full rows for candidates ──────────────────────
    id_ph = ",".join("?" * len(candidate_ids))
    id_list = list(candidate_ids)

    fetch_conditions = [f"id IN ({id_ph})", "user_id = ?"]
    fetch_params: list = id_list + [user_id]
    if category:
        fetch_conditions.append("category = ?")
        fetch_params.append(category)
    if exclude:
        ex_ph = ",".join("?" * len(exclude))
        fetch_conditions.append(f"category NOT IN ({ex_ph})")
        fetch_params.extend(exclude)

    rows = conn.execute(
        "SELECT id, category, content, importance, access_count, source, "
        "last_accessed, embedding, created_at, updated_at "
        f"FROM memory_items WHERE {' AND '.join(fetch_conditions)}",
        fetch_params,
    ).fetchall()

    # ── Phase 4: Hybrid scoring ──────────────────────────────────────
    scored: list[dict] = []
    half_life = RECALL_DECAY_HALF_LIFE_DAYS or 30.0

    for r in rows:
        rid = r["id"]

        # Recency decay
        try:
            updated = datetime.fromisoformat(r["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=TZ)
            days_ago = max((now - updated).total_seconds() / 86400, 0)
        except (ValueError, TypeError):
            days_ago = 365
        decay = math.exp(-math.log(2) * days_ago / half_life)

        # Base score from importance + access
        access_bonus = min((r["access_count"] or 0) * 0.5, 3)
        base_score = (r["importance"] * 2 + access_bonus) * decay

        # Vector similarity
        vec_sim = 0.0
        if rid in vec_scores:
            vec_sim = max(1.0 - vec_scores[rid], 0.0)
        elif query_embedding and r["embedding"]:
            # Python fallback when native KNN missed this candidate
            vec_sim = _cosine_similarity(query_embedding, r["embedding"])

        # BM25 normalised
        bm25_norm = 0.0
        if rid in bm25_scores:
            bm25_norm = min(1.0, abs(bm25_scores[rid]) / 10.0)

        # Filter: no relevance signal at all
        if vec_sim < RECALL_VEC_SIM_THRESHOLD and bm25_norm == 0 and rid not in candidate_ids - set(id_list):
            # Only keep if it came from fallback or has some signal
            if rid in vec_scores and vec_sim < RECALL_VEC_SIM_THRESHOLD:
                continue

        score = (
            RECALL_VEC_SIM_WEIGHT * vec_sim
            + RECALL_BM25_WEIGHT * bm25_norm
            + base_score
            + (RECALL_KEYWORD_BOOST if bm25_norm > 0 else 0)
        )

        scored.append({
            "id": rid,
            "category": r["category"],
            "content": r["content"],
            "importance": r["importance"],
            "source": r["source"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "score": round(score, 3),
            "vec_sim": round(vec_sim, 3),
        })

    # Sort by score descending, take top `limit`
    scored.sort(key=lambda x: x["score"], reverse=True)
    result = scored[:limit]

    # Bump access_count for returned items (skip for auto-recall)
    if bump_access:
        item_ids = [m["id"] for m in result]
        if item_ids:
            ac_ph = ",".join("?" * len(item_ids))
            try:
                conn.execute(
                    f"UPDATE memory_items SET access_count = access_count + 1, "
                    f"last_accessed = ? WHERE id IN ({ac_ph})",
                    [now.isoformat()] + item_ids,
                )
                conn.commit()
            except Exception as e:
                logger.debug("access_count bump failed: %s", e)

    conn.close()
    return result


def get_all_memory_items(user_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, category, content, importance, source, "
        "access_count, last_accessed, created_at, updated_at "
        "FROM memory_items WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_memory_items(ids: list[int], deleted_by: str = "system") -> int:
    """Soft-delete memory items: copy to trash, clean indexes, then delete."""
    if not ids:
        return 0
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    placeholders = ",".join("?" * len(ids))
    # Copy to trash before deleting
    items = conn.execute(
        f"SELECT id, user_id, category, content, importance, source, created_at "
        f"FROM memory_items WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    for item in items:
        conn.execute(
            "INSERT INTO memory_trash (original_id, user_id, category, content, importance, "
            "source, deleted_by, original_created, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item["id"], item["user_id"], item["category"], item["content"],
             item["importance"], item["source"], deleted_by, item["created_at"], now),
        )
    cur = conn.execute(f"DELETE FROM memory_items WHERE id IN ({placeholders})", ids)
    count = cur.rowcount
    conn.commit()
    conn.close()
    # Clean FTS/vec indexes
    fts_delete(ids)
    vec_delete(ids)
    return count


def merge_memory_items(keep_id: int, delete_ids: list[int],
                       merged_content: str, new_importance: int | None = None) -> None:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    if new_importance is not None:
        conn.execute(
            "UPDATE memory_items SET content = ?, importance = ?, updated_at = ? WHERE id = ?",
            (merged_content, new_importance, now, keep_id),
        )
    else:
        conn.execute(
            "UPDATE memory_items SET content = ?, updated_at = ? WHERE id = ?",
            (merged_content, now, keep_id),
        )
    if delete_ids:
        placeholders = ",".join("?" * len(delete_ids))
        # Copy merged-away items to trash
        items = conn.execute(
            f"SELECT id, user_id, category, content, importance, source, created_at "
            f"FROM memory_items WHERE id IN ({placeholders})",
            delete_ids,
        ).fetchall()
        for item in items:
            conn.execute(
                "INSERT INTO memory_trash (original_id, user_id, category, content, importance, "
                "source, deleted_by, original_created, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["id"], item["user_id"], item["category"], item["content"],
                 item["importance"], item["source"], "dedup", item["created_at"], now),
            )
        conn.execute(f"DELETE FROM memory_items WHERE id IN ({placeholders})", delete_ids)
    conn.commit()
    conn.close()
    # Re-sync FTS for kept item; clean indexes for deleted
    fts_upsert(keep_id, merged_content)
    if delete_ids:
        fts_delete(delete_ids)
        vec_delete(delete_ids)


def get_stale_memory_items(user_id: int) -> list[dict]:
    """Get memory items not accessed recently with low importance.

    Uses MEMORY_DEMOTE_AFTER_DAYS from config for the staleness threshold.
    """
    from mochi.config import MEMORY_DEMOTE_AFTER_DAYS

    conn = _connect()
    cutoff = (datetime.now(TZ) - timedelta(days=MEMORY_DEMOTE_AFTER_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT id, category, content, importance, created_at, updated_at, last_accessed
           FROM memory_items
           WHERE user_id = ?
             AND importance <= 1
             AND (last_accessed < ? OR last_accessed = '')
             AND updated_at < ?""",
        (user_id, cutoff, cutoff),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def demote_memory_item(item_id: int) -> None:
    """Soft-delete a stale memory item by setting importance to 0."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET importance = 0, updated_at = ? WHERE id = ?",
        (now, item_id),
    )
    conn.commit()
    conn.close()


def list_all_memories(user_id: int, category: str = "", limit: int = 50) -> list[dict]:
    """List all memory items, optionally filtered by category."""
    conn = _connect()
    if category:
        rows = conn.execute(
            "SELECT id, category, content, importance, source, created_at, updated_at "
            "FROM memory_items WHERE user_id = ? AND category = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, category, content, importance, source, created_at, updated_at "
            "FROM memory_items WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_memory_stats(user_id: int) -> dict:
    """Get memory system statistics."""
    conn = _connect()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM memory_items WHERE user_id = ?", (user_id,)
    ).fetchone()["cnt"]
    by_cat = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM memory_items "
        "WHERE user_id = ? GROUP BY category ORDER BY cnt DESC",
        (user_id,),
    ).fetchall()
    high_imp = conn.execute(
        "SELECT COUNT(*) as cnt FROM memory_items "
        "WHERE user_id = ? AND importance >= 3", (user_id,)
    ).fetchone()["cnt"]
    conn.close()
    return {
        "total": total,
        "high_importance": high_imp,
        "categories": {r["category"]: r["cnt"] for r in by_cat},
    }


def list_memory_trash(user_id: int, limit: int = 20) -> list[dict]:
    """List recently deleted memories (trash bin)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, original_id, category, content, importance, deleted_by, deleted_at "
        "FROM memory_trash WHERE user_id = ? ORDER BY deleted_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def restore_memory_from_trash(trash_id: int, user_id: int) -> int | None:
    """Restore a memory from trash back to memory_items. Returns new item id or None."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    item = conn.execute(
        "SELECT original_id, user_id, category, content, importance, source, original_created "
        "FROM memory_trash WHERE id = ? AND user_id = ?",
        (trash_id, user_id),
    ).fetchone()
    if not item:
        conn.close()
        return None
    cursor = conn.execute(
        "INSERT INTO memory_items (user_id, category, content, importance, access_count, "
        "source, created_at, updated_at, last_accessed) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (item["user_id"], item["category"], item["content"], item["importance"],
         item["source"], item["original_created"], now, now),
    )
    new_id = cursor.lastrowid
    conn.execute("DELETE FROM memory_trash WHERE id = ?", (trash_id,))
    conn.commit()
    conn.close()
    # Re-sync FTS index for restored item
    fts_upsert(new_id, item["content"])
    return new_id


def cleanup_old_trash(days: int = 30) -> int:
    """Permanently delete trash items older than N days. Returns count purged."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM memory_trash WHERE deleted_at < ?", (cutoff,)
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def update_memory_importance(item_id: int, new_importance: int) -> None:
    """Update importance level of a memory item."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET importance = ?, updated_at = ? WHERE id = ?",
        (new_importance, now, item_id),
    )
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
            "today": {"by_model": {model: {"prompt": int, "completion": int}, ...}},
            "month": {"by_model": {model: {"prompt": int, "completion": int}, ...}},
        }
    """
    now = datetime.now(TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    conn = _connect()

    def _by_model(since: str) -> dict:
        result = {}
        for r in conn.execute(
            """SELECT model,
                      COALESCE(SUM(prompt_tokens), 0) as p,
                      COALESCE(SUM(completion_tokens), 0) as c
               FROM usage_log WHERE created_at >= ? GROUP BY model""",
            (since,),
        ).fetchall():
            result[r["model"] or "unknown"] = {"prompt": r["p"], "completion": r["c"]}
        return result

    today = {"by_model": _by_model(today_start)}
    month = {"by_model": _by_model(month_start)}

    conn.close()
    return {"today": today, "month": month}


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


def get_awake_tick_count_today() -> int:
    """Count today's awake heartbeat ticks (Think actually ran).

    Used to detect first tick of the day for morning briefing.
    Excludes passive/sleeping actions so SLEEPING-state ticks don't count.
    """
    from mochi.config import logical_today
    today = logical_today()
    conn = _connect()
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM heartbeat_log "
        "WHERE action NOT IN "
        "('sleeping','observe_only','silent_pause',"
        "'maintenance','maintenance_error') "
        "AND created_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ═══════════════════════════════════════════════════════════════════════════
# Proactive Log — tracks sent proactive messages for Think dedup
# ═══════════════════════════════════════════════════════════════════════════

def log_proactive(content: str, msg_type: str = "proactive") -> None:
    """Record a proactive message that was sent to the user."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO proactive_log (type, content, created_at) VALUES (?, ?, ?)",
        (msg_type, content, now),
    )
    conn.commit()
    conn.close()


def get_today_proactive_sent() -> list[dict]:
    """Return today's proactive messages (up to 5, newest first).

    Uses logical_today() for day boundary consistency with diary/heartbeat.
    Each entry: {"type": topic, "content": first 80 chars, "time": HH:MM}.
    """
    from mochi.config import logical_today
    today = logical_today()
    conn = _connect()
    rows = conn.execute(
        "SELECT type, content, created_at FROM proactive_log "
        "WHERE created_at LIKE ? ORDER BY id DESC LIMIT 5",
        (f"{today}%",),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        time_str = ""
        try:
            dt = datetime.fromisoformat(r["created_at"])
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        result.append({
            "type": r["type"],
            "content": r["content"][:80],
            "time": time_str,
        })
    return result


def cleanup_proactive_log(days: int = 30) -> int:
    """Delete proactive_log entries older than N days. Returns count deleted."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM proactive_log WHERE created_at < ?", (cutoff,)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


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


def get_skill_config(skill_name: str) -> dict[str, str]:
    """Return all config key-value pairs for a skill (excluding internal keys like _enabled)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT key, value FROM skill_config "
        "WHERE skill_name = ? AND key NOT LIKE '\\_%' ESCAPE '\\'",
        (skill_name,),
    ).fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_skill_config(skill_name: str, key: str, value: str) -> None:
    """Set a config value for a skill (upsert)."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO skill_config (skill_name, key, value, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(skill_name, key) DO UPDATE SET value = ?, updated_at = ?",
        (skill_name, key, value, now, value, now),
    )
    conn.commit()
    conn.close()


def delete_skill_config(skill_name: str, key: str) -> None:
    """Delete a config value for a skill."""
    conn = _connect()
    conn.execute(
        "DELETE FROM skill_config WHERE skill_name = ? AND key = ?",
        (skill_name, key),
    )
    conn.commit()
    conn.close()
