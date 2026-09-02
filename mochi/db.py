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
from datetime import datetime, timedelta

from mochi.config import (
    DB_PATH, TZ,
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
            created_at TEXT    NOT NULL,
            turn_id    TEXT    DEFAULT NULL
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
            evidence_message_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT    NOT NULL,
            updated_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_items_user
            ON memory_items(user_id);

        -- Durable cursor for continuous Lite memory extraction.
        CREATE TABLE IF NOT EXISTS memory_extraction_state (
            user_id                   INTEGER PRIMARY KEY,
            last_processed_message_id INTEGER NOT NULL DEFAULT 0,
            last_success_at           TEXT DEFAULT NULL,
            last_error                TEXT DEFAULT NULL,
            updated_at                TEXT NOT NULL
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

        CREATE TABLE IF NOT EXISTS scheduled_runs (
            job_name      TEXT NOT NULL,
            period_key    TEXT NOT NULL,
            status        TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
            attempt_count INTEGER NOT NULL DEFAULT 1,
            started_at    TEXT NOT NULL,
            finished_at   TEXT DEFAULT NULL,
            error         TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(job_name, period_key)
        );

        CREATE TABLE IF NOT EXISTS heartbeat_schedules (
            entry_kind TEXT PRIMARY KEY,
            next_due_at TEXT NOT NULL,
            wake_reason TEXT NOT NULL DEFAULT 'periodic',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS heartbeat_runs (
            run_key             TEXT PRIMARY KEY,
            entry_kind          TEXT NOT NULL,
            user_id             INTEGER NOT NULL,
            channel_id          INTEGER NOT NULL,
            transport           TEXT NOT NULL,
            wake_reason         TEXT NOT NULL,
            facts_json          TEXT NOT NULL DEFAULT '[]',
            status              TEXT NOT NULL DEFAULT 'pending',
            claim_token         TEXT DEFAULT NULL,
            lease_until         TEXT DEFAULT NULL,
            result_json         TEXT DEFAULT NULL,
            outcome             TEXT DEFAULT NULL,
            attempt_count       INTEGER NOT NULL DEFAULT 0,
            next_attempt_at     TEXT DEFAULT NULL,
            last_error          TEXT NOT NULL DEFAULT '',
            delivery_started_at TEXT DEFAULT NULL,
            text_delivered_at   TEXT DEFAULT NULL,
            created_at          TEXT NOT NULL,
            handled_at          TEXT DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_heartbeat_runs_due
            ON heartbeat_runs(status, next_attempt_at, lease_until);

        CREATE TABLE IF NOT EXISTS attention_facts (
            source       TEXT NOT NULL,
            stable_key   TEXT NOT NULL,
            observed_at  TEXT NOT NULL,
            fresh_until  TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'unresolved',
            facts_json   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY(source, stable_key)
        );
        CREATE INDEX IF NOT EXISTS idx_attention_facts_status
            ON attention_facts(status, observed_at DESC);

        CREATE TABLE IF NOT EXISTS weekly_curation_batches (
            user_id            INTEGER NOT NULL,
            period_key        TEXT    NOT NULL,
            result_json       TEXT    NOT NULL,
            created_at        TEXT    NOT NULL,
            PRIMARY KEY(user_id, period_key)
        );

        -- Durable truth for tool executions.  Conversation history may show a
        -- compact projection of these facts, but never reconstructs fake
        -- provider-native tool messages from them.
        CREATE TABLE IF NOT EXISTS tool_executions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id          TEXT    NOT NULL,
            tool_call_id     TEXT    NOT NULL DEFAULT '',
            user_id          INTEGER NOT NULL,
            source           TEXT    NOT NULL DEFAULT 'chat',
            skill_name       TEXT    NOT NULL DEFAULT '',
            tool_name        TEXT    NOT NULL,
            action           TEXT    NOT NULL DEFAULT '',
            arguments_json   TEXT    NOT NULL DEFAULT '{}',
            status           TEXT    NOT NULL DEFAULT 'running',
            result_summary   TEXT    NOT NULL DEFAULT '',
            entity_refs_json TEXT    NOT NULL DEFAULT '[]',
            state_changed    INTEGER NOT NULL DEFAULT 0,
            started_at       TEXT    NOT NULL,
            finished_at      TEXT    DEFAULT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_exec_user_time
            ON tool_executions(user_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tool_exec_turn
            ON tool_executions(turn_id, id);

        -- Proactive message history
        CREATE TABLE IF NOT EXISTS proactive_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            type       TEXT    NOT NULL DEFAULT 'proactive',
            content    TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_proactive_created ON proactive_log(created_at);

        -- Soft-delete archive for memory items
        CREATE TABLE IF NOT EXISTS memory_trash (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id      INTEGER NOT NULL,
            user_id          INTEGER NOT NULL,
            category         TEXT    NOT NULL DEFAULT '',
            content          TEXT    NOT NULL,
            importance       INTEGER NOT NULL DEFAULT 1,
            source           TEXT    NOT NULL DEFAULT 'chat',
            evidence_message_ids TEXT NOT NULL DEFAULT '[]',
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
            entity_type  TEXT    NOT NULL
                         CHECK(entity_type IN ('person', 'pet', 'place')),
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
            source_memory_id INTEGER DEFAULT NULL,
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
        -- Continuous summary state.
        CREATE TABLE IF NOT EXISTS conversation_summary_state (
            user_id            INTEGER PRIMARY KEY,
            reset_at           TEXT DEFAULT NULL,
            summary            TEXT NOT NULL DEFAULT '',
            through_message_id INTEGER NOT NULL DEFAULT 0,
            last_success_at     TEXT DEFAULT NULL,
            last_error          TEXT DEFAULT NULL,
            updated_at          TEXT NOT NULL
        );

        -- Per-user context reset boundary (set by /reset command)
        CREATE TABLE IF NOT EXISTS conversation_reset (
            user_id   INTEGER PRIMARY KEY,
            reset_at  TEXT    NOT NULL
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
    _add_col("messages", "turn_id", "TEXT DEFAULT NULL")

    # memory_items
    _add_col("memory_items", "access_count", "INTEGER NOT NULL DEFAULT 0")
    _add_col("memory_items", "last_accessed", "TEXT NOT NULL DEFAULT ''")
    _add_col("memory_items", "embedding", "BLOB DEFAULT NULL")
    _add_col(
        "memory_items", "evidence_message_ids",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _add_col(
        "memory_trash", "evidence_message_ids",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    had_kg_provenance = _has_col("kg_triples", "source_memory_id")
    _add_col("kg_triples", "source_memory_id", "INTEGER DEFAULT NULL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kg_triple_source_memory "
        "ON kg_triples(source_memory_id, valid_to)"
    )
    if not had_kg_provenance:
        retired_at = datetime.now(TZ).isoformat()
        retired = conn.execute(
            "UPDATE kg_triples SET valid_to = ? "
            "WHERE source_memory_id IS NULL AND valid_to IS NULL",
            (retired_at,),
        ).rowcount
        if retired:
            logger.info(
                "Retired %d legacy KG triple(s) without Memory provenance",
                retired,
            )

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
        ("reasoning_tokens", "INTEGER DEFAULT NULL"),
        ("cached_prompt_tokens", "INTEGER DEFAULT NULL"),
    ]:
        _add_col("usage_log", col, typedef)

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
    try:
        conn.execute("SELECT COUNT(*) FROM memory_items_fts")
        _FTS_AVAILABLE = True
        _repair_memory_fts_index(conn)
    except Exception:
        _FTS_AVAILABLE = False


def _repair_memory_fts_index(conn: sqlite3.Connection) -> None:
    """Repair drift against authoritative Memory Items only when needed."""
    expected = {
        row["id"]: _fts_tokenize(row["content"])
        for row in conn.execute(
            "SELECT id, content FROM memory_items"
        ).fetchall()
    }
    actual = {
        row["rowid"]: row["content"]
        for row in conn.execute(
            "SELECT rowid, content FROM memory_items_fts"
        ).fetchall()
    }
    if actual == expected:
        return
    conn.execute("DELETE FROM memory_items_fts")
    for item_id, content in expected.items():
        conn.execute(
            "INSERT INTO memory_items_fts(rowid, content) VALUES (?, ?)",
            (item_id, content),
        )
    conn.commit()
    logger.info("Repaired memory FTS index (%d rows)", len(expected))


def _get_embed_dim() -> int:
    """Get embedding dimension: probed from model pool if available, else config fallback."""
    try:
        from mochi.model_pool import get_pool
        pool = get_pool()
        dim = pool.get_embed_dim()
        if dim:
            return dim
    except Exception:
        pass
    from mochi.config import VEC_EMBEDDING_DIM
    return VEC_EMBEDDING_DIM


def _get_vec_table_dim(conn: sqlite3.Connection) -> int | None:
    """Read the dimension of existing vec_memories table from its schema SQL."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_memories'"
    ).fetchone()
    if not row or not row[0]:
        return None
    m = re.search(r'float\[(\d+)\]', row[0])
    return int(m.group(1)) if m else None


def _init_vec(conn: sqlite3.Connection) -> None:
    """Initialize sqlite-vec virtual table for native vector KNN (optional)."""
    global _VEC_AVAILABLE
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)

        target_dim = _get_embed_dim()

        vec_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_memories'"
        ).fetchone()

        if vec_exists:
            current_dim = _get_vec_table_dim(conn)
            if current_dim and current_dim != target_dim:
                logger.warning(
                    "vec_memories dimension mismatch: table=%d, model=%d — rebuilding",
                    current_dim, target_dim,
                )
                conn.execute("DROP TABLE vec_memories")
                vec_exists = None  # fall through to creation + backfill

        if not vec_exists:
            conn.execute(
                f"CREATE VIRTUAL TABLE vec_memories USING vec0("
                f"item_id INTEGER PRIMARY KEY, "
                f"embedding float[{target_dim}] distance_metric=cosine)"
            )
            count = 0
            for r in conn.execute(
                "SELECT id, embedding FROM memory_items WHERE embedding IS NOT NULL"
            ).fetchall():
                emb = r["embedding"]
                emb_dim = len(emb) // 4 if emb else 0
                if emb_dim == target_dim:
                    conn.execute(
                        "INSERT INTO vec_memories(item_id, embedding) VALUES (?, ?)",
                        (r["id"], emb),
                    )
                    count += 1
            conn.commit()
            if count:
                logger.info("Created vec_memories (dim=%d) and backfilled %d rows", target_dim, count)
            else:
                logger.info("Created vec_memories (dim=%d), no matching embeddings to backfill", target_dim)

        _VEC_AVAILABLE = True
        logger.info("sqlite-vec loaded, native vector search enabled (dim=%d)", target_dim)
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


def _fts_query_tokens(text: str) -> list[str]:
    """Return unique query terms without one-letter ASCII noise."""
    return list(dict.fromkeys(
        token
        for token in _fts_tokenize(text).split()
        if not (token.isascii() and len(token) < 2)
    ))


def _fts_match_is_relevant(query_tokens: list[str], content: str) -> bool:
    """Conservatively gate rows returned by broad OR-based FTS."""
    if not query_tokens:
        return False
    overlap = set(query_tokens) & set(_fts_tokenize(content).split())
    if len(query_tokens) == 1:
        return bool(overlap)
    if len(overlap) >= 2:
        return True
    return any(token.isascii() and len(token) >= 5 for token in overlap)


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
        if not own_conn:
            raise
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
        if not own_conn:
            raise
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
        if not own_conn:
            raise
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
        if not own_conn:
            raise
    finally:
        if own_conn:
            conn.close()

def save_message(user_id: int, role: str, content: str,
                 tool_history: str | None = None,
                 turn_id: str | None = None,
                 processed: bool = False) -> int:
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, tool_history, turn_id, processed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, role, content, now, tool_history, turn_id,
            int(processed),
        ),
    )
    conn.commit()
    conn.close()
    return int(cursor.lastrowid)


def save_message_once(
    user_id: int,
    role: str,
    content: str,
    *,
    tool_history: str | None = None,
    turn_id: str,
    processed: bool = False,
) -> bool:
    """Idempotently persist one role within a stable turn."""
    if not turn_id:
        raise ValueError("turn_id is required for idempotent message writes")
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM messages "
            "WHERE user_id = ? AND role = ? AND turn_id = ? LIMIT 1",
            (user_id, role, turn_id),
        ).fetchone()
        if existing:
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO messages "
            "(user_id, role, content, created_at, tool_history, turn_id, processed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, role, content, now, tool_history, turn_id,
                int(processed),
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_recent_messages(user_id: int, limit: int = 20, since: str | None = None) -> list[dict]:
    conn = _connect()
    if since:
        rows = conn.execute(
            "SELECT role, content, created_at, tool_history, turn_id FROM messages"
            " WHERE user_id = ? AND created_at > ? ORDER BY id DESC LIMIT ?",
            (user_id, since, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT role, content, created_at, tool_history, turn_id FROM messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


def get_recent_user_messages_in_window(
    user_id: int,
    start: datetime,
    end: datetime,
    *,
    limit: int = 20,
    excerpt_chars: int = 200,
) -> list[dict]:
    """Return bounded same-user evidence messages from one instant window."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 0:
        raise ValueError("user_id must be a non-negative integer")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("message window must be timezone-aware and increasing")
    limit = max(1, min(int(limit), 20))
    excerpt_chars = max(1, min(int(excerpt_chars), 200))
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, content, created_at FROM messages "
            "WHERE user_id = ? AND role = 'user' "
            "AND julianday(created_at) >= julianday(?) "
            "AND julianday(created_at) < julianday(?) "
            "ORDER BY julianday(created_at) DESC, id DESC LIMIT ?",
            (user_id, start.isoformat(), end.isoformat(), limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "content": row["content"][:excerpt_chars],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


def start_tool_execution(*, turn_id: str, tool_call_id: str, user_id: int,
                         source: str, skill_name: str, tool_name: str,
                         action: str, arguments_json: str) -> int:
    """Create a durable tool execution record before dispatch starts."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO tool_executions "
        "(turn_id, tool_call_id, user_id, source, skill_name, tool_name, action, "
        " arguments_json, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)",
        (turn_id, tool_call_id, user_id, source, skill_name, tool_name,
         action, arguments_json, now),
    )
    execution_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return execution_id


def finish_tool_execution(execution_id: int, *, status: str,
                          result_summary: str = "",
                          entity_refs: list[str] | None = None,
                          state_changed: bool = False) -> None:
    """Finalize a tool execution with its real outcome."""
    now = datetime.now(TZ).isoformat()
    refs_json = json.dumps(entity_refs or [], ensure_ascii=False)
    conn = _connect()
    conn.execute(
        "UPDATE tool_executions SET status = ?, result_summary = ?, "
        "entity_refs_json = ?, state_changed = ?, finished_at = ? WHERE id = ?",
        (status, result_summary, refs_json, int(state_changed), now, execution_id),
    )
    conn.commit()
    conn.close()


def get_recent_tool_executions(user_id: int, *, hours: int = 24,
                               limit: int = 3,
                               skill_names: list[str] | None = None,
                               state_changes_only: bool = True) -> list[dict]:
    """Return recent real tool facts for contextual follow-up resolution."""
    cutoff = (datetime.now(TZ) - timedelta(hours=max(1, hours))).isoformat()
    conditions = ["user_id = ?", "started_at >= ?", "status = 'success'"]
    params: list = [user_id, cutoff]
    if state_changes_only:
        conditions.append("state_changed = 1")
    if skill_names:
        normalized = [s for s in skill_names if s]
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            conditions.append(f"skill_name IN ({placeholders})")
            params.extend(normalized)
    params.append(max(1, limit))
    conn = _connect()
    rows = conn.execute(
        "SELECT id, turn_id, tool_call_id, user_id, source, skill_name, "
        "tool_name, action, arguments_json, status, result_summary, "
        "entity_refs_json, state_changed, started_at, finished_at "
        "FROM tool_executions WHERE " + " AND ".join(conditions) +
        " ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["arguments"] = json.loads(item.pop("arguments_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["arguments"] = {}
            item.pop("arguments_json", None)
        try:
            item["entity_refs"] = json.loads(item.pop("entity_refs_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["entity_refs"] = []
            item.pop("entity_refs_json", None)
        item["state_changed"] = bool(item["state_changed"])
        result.append(item)
    return result


def get_tool_executions_for_turn(turn_id: str) -> list[dict]:
    """Return every durable tool attempt for one stable runtime turn."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, tool_name, status, state_changed, result_summary "
        "FROM tool_executions WHERE turn_id = ? ORDER BY id",
        (turn_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def set_context_reset(user_id: int) -> str:
    """Record a reset boundary for *user_id* at the current time.

    Subsequent ``get_recent_messages`` calls passing ``since=get_context_reset(user_id)``
    will only return messages created after this timestamp. Original messages
    are preserved in the DB.
    """
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO conversation_reset (user_id, reset_at) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET reset_at = excluded.reset_at",
            (user_id, now),
        )
        conn.execute(
            "INSERT INTO conversation_summary_state "
            "(user_id, reset_at, summary, through_message_id, updated_at) "
            "VALUES (?, ?, '', 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "reset_at = excluded.reset_at, summary = '', through_message_id = 0, "
            "last_success_at = NULL, last_error = NULL, "
            "updated_at = excluded.updated_at",
            (user_id, now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return now


def get_context_reset(user_id: int) -> str | None:
    """Return the ISO timestamp of the most recent reset boundary, or None."""
    conn = _connect()
    row = conn.execute(
        "SELECT reset_at FROM conversation_reset WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["reset_at"] if row else None


def _eligible_conversation_messages(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    after_message_id: int = 0,
    reset_at: str | None = None,
) -> list[dict]:
    conditions = ["user_id = ?", "id > ?"]
    params: list = [user_id, after_message_id]
    if reset_at is not None:
        conditions.append("created_at > ?")
        params.append(reset_at)
    rows = conn.execute(
        "SELECT id, role, content, created_at, tool_history, turn_id, processed "
        "FROM messages WHERE " + " AND ".join(conditions) + " ORDER BY id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _pair_conversation_turns(messages: list[dict]) -> list[dict]:
    """Pair eligible ordinary turns without inventing or changing roles."""
    modern_users: dict[str, dict] = {}
    legacy_user: dict | None = None
    turns: list[dict] = []

    for message in messages:
        role = message["role"]
        processed = bool(message.get("processed"))
        turn_id = message.get("turn_id")

        if processed or role not in {"user", "assistant"}:
            legacy_user = None
            continue
        if role == "user":
            if turn_id:
                modern_users.setdefault(turn_id, message)
                legacy_user = None
            else:
                legacy_user = message
            continue

        user_message = None
        if turn_id:
            user_message = modern_users.pop(turn_id, None)
            legacy_user = None
        elif legacy_user is not None:
            user_message = legacy_user
            legacy_user = None
        if user_message is not None:
            turns.append({
                "user": user_message,
                "assistant": message,
                "through_message_id": message["id"],
            })

    turns.sort(key=lambda turn: turn["through_message_id"])
    return turns


def _ensure_memory_extraction_state(
    conn: sqlite3.Connection, user_id: int,
) -> sqlite3.Row:
    """Initialize the durable cursor from the newest valid legacy marker."""
    row = conn.execute(
        "SELECT * FROM memory_extraction_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is not None:
        return row

    legacy_cursor = 0
    markers = conn.execute(
        "SELECT content FROM messages WHERE user_id = ? AND role = 'system' "
        "AND content LIKE '[memory_extracted]%' ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    for marker in markers:
        match = re.search(r"\bup_to_id=(\d+)\b", marker["content"] or "")
        if match:
            legacy_cursor = int(match.group(1))
            break
    now = datetime.now(TZ).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO memory_extraction_state "
        "(user_id, last_processed_message_id, updated_at) VALUES (?, ?, ?)",
        (user_id, legacy_cursor, now),
    )
    return conn.execute(
        "SELECT * FROM memory_extraction_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()


def get_memory_extraction_batch(
    user_id: int, batch_turns: int = 10,
) -> tuple[int, list[dict]]:
    """Return the exact next complete ordinary-turn batch after the cursor."""
    conn = _connect()
    try:
        state = _ensure_memory_extraction_state(conn, user_id)
        turns = [
            turn for turn in _pair_conversation_turns(
                _eligible_conversation_messages(conn, user_id)
            )
            if (
                turn["through_message_id"]
                > state["last_processed_message_id"]
            )
        ]
        conn.commit()
        size = max(1, int(batch_turns))
        if len(turns) < size:
            return state["last_processed_message_id"], []
        messages: list[dict] = []
        for turn in turns[:size]:
            messages.extend((turn["user"], turn["assistant"]))
        return state["last_processed_message_id"], messages
    finally:
        conn.close()


def get_memory_extraction_status(
    user_id: int, batch_turns: int = 10,
) -> dict:
    """Return the extraction cursor and complete pending turns."""
    conn = _connect()
    try:
        state = _ensure_memory_extraction_state(conn, user_id)
        pending_turns = len([
            turn for turn in _pair_conversation_turns(
                _eligible_conversation_messages(conn, user_id)
            )
            if (
                turn["through_message_id"]
                > state["last_processed_message_id"]
            )
        ])
        conn.commit()
        result = dict(state)
        result.update({
            "pending_turns": pending_turns,
            "pending_messages": pending_turns * 2,
            "batch_turns": max(1, int(batch_turns)),
            "threshold": max(1, int(batch_turns)) * 2,
        })
        return result
    finally:
        conn.close()


def list_memory_extraction_users() -> list[int]:
    """Return users with chat history."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT user_id FROM messages ORDER BY user_id"
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    finally:
        conn.close()


def record_memory_extraction_error(user_id: int, error: str) -> None:
    """Persist a bounded diagnostic without moving the extraction cursor."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        _ensure_memory_extraction_state(conn, user_id)
        conn.execute(
            "UPDATE memory_extraction_state SET last_error = ?, updated_at = ? "
            "WHERE user_id = ?",
            (str(error)[:1000], now, user_id),
        )
        conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Continuous Conversation Summary
# ═══════════════════════════════════════════════════════════════════════════


def _current_context_reset(
    conn: sqlite3.Connection, user_id: int,
) -> str | None:
    row = conn.execute(
        "SELECT reset_at FROM conversation_reset WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["reset_at"] if row else None


def _ensure_conversation_summary_state(
    conn: sqlite3.Connection, user_id: int,
) -> sqlite3.Row:
    """Return durable state aligned with the current reset epoch."""
    reset_at = _current_context_reset(conn, user_id)
    row = conn.execute(
        "SELECT * FROM conversation_summary_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    now = datetime.now(TZ).isoformat()
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO conversation_summary_state "
            "(user_id, reset_at, summary, through_message_id, updated_at) "
            "VALUES (?, ?, '', 0, ?)",
            (user_id, reset_at, now),
        )
    elif row["reset_at"] != reset_at:
        conn.execute(
            "UPDATE conversation_summary_state SET reset_at = ?, summary = '', "
            "through_message_id = 0, last_success_at = NULL, last_error = NULL, "
            "updated_at = ? WHERE user_id = ?",
            (reset_at, now, user_id),
        )
    return conn.execute(
        "SELECT * FROM conversation_summary_state WHERE user_id = ?",
        (user_id,),
    ).fetchone()


def get_conversation_summary_batch(
    user_id: int, batch_turns: int = 20,
) -> dict | None:
    """Return the exact next complete-turn batch without advancing state."""
    conn = _connect()
    try:
        state = _ensure_conversation_summary_state(conn, user_id)
        turns = [
            turn for turn in _pair_conversation_turns(
                _eligible_conversation_messages(
                    conn, user_id, reset_at=state["reset_at"],
                )
            )
            if turn["through_message_id"] > state["through_message_id"]
        ]
        conn.commit()
        size = max(1, int(batch_turns))
        if len(turns) < size:
            return None
        batch = turns[:size]
        return {
            "user_id": user_id,
            "reset_at": state["reset_at"],
            "summary": state["summary"],
            "through_message_id": state["through_message_id"],
            "next_through_message_id": batch[-1]["through_message_id"],
            "turns": batch,
        }
    finally:
        conn.close()


def save_conversation_summary(claim: dict, summary: str) -> bool:
    """Atomically advance only if reset epoch and cursor still match."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT * FROM conversation_summary_state WHERE user_id = ?",
            (claim["user_id"],),
        ).fetchone()
        if (
            state is None
            or state["reset_at"] != claim["reset_at"]
            or state["through_message_id"] != claim["through_message_id"]
            or _current_context_reset(conn, claim["user_id"]) != claim["reset_at"]
        ):
            conn.rollback()
            return False
        conn.execute(
            "UPDATE conversation_summary_state SET summary = ?, "
            "through_message_id = ?, last_success_at = ?, last_error = NULL, "
            "updated_at = ? WHERE user_id = ?",
            (
                summary,
                claim["next_through_message_id"],
                now,
                now,
                claim["user_id"],
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_conversation_summary_error(claim: dict, error: str) -> bool:
    """Record failure for the same durable claim without moving its cursor."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT * FROM conversation_summary_state WHERE user_id = ?",
            (claim["user_id"],),
        ).fetchone()
        if (
            state is None
            or state["reset_at"] != claim["reset_at"]
            or state["through_message_id"] != claim["through_message_id"]
            or _current_context_reset(conn, claim["user_id"]) != claim["reset_at"]
        ):
            conn.rollback()
            return False
        conn.execute(
            "UPDATE conversation_summary_state SET last_error = ?, updated_at = ? "
            "WHERE user_id = ?",
            (str(error)[:1000], now, claim["user_id"]),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_conversation_context(
    user_id: int,
    recent_turns: int = 10,
    *,
    include_summary: bool = True,
) -> dict:
    """Return role-true recent context plus every not-yet-summarized turn."""
    conn = _connect()
    try:
        if include_summary:
            state = _ensure_conversation_summary_state(conn, user_id)
            reset_at = state["reset_at"]
            summary = state["summary"]
            through_message_id = state["through_message_id"]
        else:
            reset_at = _current_context_reset(conn, user_id)
            summary = ""
            through_message_id = 0

        messages = _eligible_conversation_messages(
            conn, user_id, reset_at=reset_at,
        )
        turns = _pair_conversation_turns(messages)
        recent = turns[-max(1, int(recent_turns)):]
        recent_ids = {turn["through_message_id"] for turn in recent}
        overflow = (
            [
                turn for turn in turns
                if turn["through_message_id"] > through_message_id
                and turn["through_message_id"] not in recent_ids
            ]
            if include_summary
            else []
        )

        paired_user_ids = {turn["user"]["id"] for turn in turns}
        trailing = []
        if messages:
            last = messages[-1]
            if (
                last["role"] == "user"
                and not last["processed"]
                and last["id"] not in paired_user_ids
            ):
                trailing.append(last)

        def _flatten(selected: list[dict]) -> list[dict]:
            flattened: list[dict] = []
            for turn in selected:
                flattened.extend((turn["user"], turn["assistant"]))
            return flattened

        conn.commit()
        return {
            "summary": summary,
            "through_message_id": through_message_id,
            "overflow": _flatten(overflow),
            "recent": _flatten(recent),
            "trailing": trailing,
        }
    finally:
        conn.close()


def get_conversation_summary_status(
    user_id: int, batch_turns: int = 20,
) -> dict:
    """Expose cursor, complete pending turns, and the latest worker result."""
    conn = _connect()
    try:
        state = _ensure_conversation_summary_state(conn, user_id)
        pending = sum(
            turn["through_message_id"] > state["through_message_id"]
            for turn in _pair_conversation_turns(
                _eligible_conversation_messages(
                    conn, user_id, reset_at=state["reset_at"],
                )
            )
        )
        conn.commit()
        result = dict(state)
        result.update({
            "pending_turns": pending,
            "batch_turns": max(1, int(batch_turns)),
        })
        return result
    finally:
        conn.close()


def list_conversation_summary_users() -> list[int]:
    """Return users with messages in their current reset epoch."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT m.user_id FROM messages AS m "
            "LEFT JOIN conversation_reset AS r ON r.user_id = m.user_id "
            "WHERE r.reset_at IS NULL OR m.created_at > r.reset_at "
            "ORDER BY m.user_id"
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Memory Items (Layer 2)
# ═══════════════════════════════════════════════════════════════════════════

_CORE_MEMORY_DEDUP_RATIO = 0.85


def _normalize_text(text: str) -> str:
    """Normalize text for similarity comparison: NFKC + lowercase + alphanum/CJK only."""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in normalized if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def text_similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 similarity ratio between two strings after normalization."""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _memory_contents_match(content: str, existing_content: str) -> bool:
    normalized = _normalize_text(content)
    existing = _normalize_text(existing_content)
    if not normalized or not existing:
        return False
    shorter = min(len(normalized), len(existing))
    return (
        normalized == existing
        or (
            shorter >= 6
            and (normalized in existing or existing in normalized)
        )
        or (
            shorter >= 8
            and difflib.SequenceMatcher(None, normalized, existing).ratio()
            >= 0.94
        )
    )


def _find_memory_duplicate_in_rows(
    content: str, rows: list[sqlite3.Row | dict],
) -> dict | None:
    for row in rows:
        if _memory_contents_match(content, row["content"]):
            return dict(row)
    return None


def _sync_memory_item_indexes(
    conn: sqlite3.Connection,
    item_id: int,
    content: str,
    embedding: bytes | None,
) -> None:
    conn.execute(
        "UPDATE memory_items SET embedding = ? WHERE id = ?",
        (embedding, item_id),
    )
    fts_upsert(item_id, content, conn)
    if embedding is not None:
        vec_upsert(item_id, embedding, conn)
    else:
        vec_delete([item_id], conn)


def _delete_memory_item_indexes(
    conn: sqlite3.Connection,
    item_ids: list[int],
) -> None:
    fts_delete(item_ids, conn)
    vec_delete(item_ids, conn)


def _invalidate_memory_kg_indexes(
    conn: sqlite3.Connection,
    item_ids: list[int],
) -> None:
    if not item_ids:
        return
    placeholders = ",".join("?" * len(item_ids))
    conn.execute(
        f"UPDATE kg_triples SET valid_to = ? "
        f"WHERE source_memory_id IN ({placeholders}) AND valid_to IS NULL",
        [datetime.now(TZ).isoformat(), *item_ids],
    )


def _insert_memory_trash_snapshot(
    conn: sqlite3.Connection,
    item: sqlite3.Row | dict,
    *,
    deleted_by: str,
    deleted_at: str,
    evidence_message_ids: str | None = None,
) -> int:
    evidence = (
        evidence_message_ids
        if evidence_message_ids is not None
        else item["evidence_message_ids"]
    )
    cursor = conn.execute(
        "INSERT INTO memory_trash "
        "(original_id, user_id, category, content, importance, source, "
        "evidence_message_ids, deleted_by, original_created, deleted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item["id"], item["user_id"], item["category"], item["content"],
            item["importance"], item["source"], evidence, deleted_by,
            item["created_at"], deleted_at,
        ),
    )
    return cursor.lastrowid


def insert_memory_item(
    user_id: int,
    content: str,
    importance: int,
    *,
    source: str,
    embedding: bytes | None = None,
    evidence_message_ids: list[int] | tuple[int, ...] | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Insert one Memory Item without rewriting any existing item."""
    from mochi.memory_contract import encode_evidence_message_ids

    own_conn = conn is None
    if own_conn:
        conn = _connect()
    now = datetime.now(TZ).isoformat()
    evidence_json = encode_evidence_message_ids(evidence_message_ids)
    cursor = conn.execute(
        "INSERT INTO memory_items "
        "(user_id, category, content, importance, source, created_at, updated_at, "
        "embedding, evidence_message_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, "", content, importance, source, now, now,
            embedding, evidence_json,
        ),
    )
    item_id = cursor.lastrowid
    _sync_memory_item_indexes(conn, item_id, content, embedding)
    if own_conn:
        conn.commit()
        conn.close()
    return item_id


def commit_memory_extraction_batch(
    user_id: int,
    *,
    expected_cursor: int,
    through_message_id: int,
    batch_user_message_ids: list[int],
    memories: list[dict],
) -> list[int]:
    """Insert validated items and advance the extraction cursor together."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = _ensure_memory_extraction_state(conn, user_id)
        actual_cursor = state["last_processed_message_id"]
        if actual_cursor != expected_cursor:
            raise RuntimeError(
                f"memory extraction cursor changed: expected {expected_cursor}, "
                f"found {actual_cursor}"
            )
        boundary = conn.execute(
            "SELECT role, processed FROM messages "
            "WHERE id = ? AND user_id = ?",
            (through_message_id, user_id),
        ).fetchone()
        if (
            boundary is None
            or boundary["role"] != "assistant"
            or boundary["processed"]
            or through_message_id <= expected_cursor
        ):
            raise ValueError("memory extraction boundary is not an eligible assistant")

        if (
            not isinstance(batch_user_message_ids, list)
            or not batch_user_message_ids
            or any(
                isinstance(message_id, bool)
                or not isinstance(message_id, int)
                for message_id in batch_user_message_ids
            )
            or len(set(batch_user_message_ids)) != len(batch_user_message_ids)
        ):
            raise ValueError(
                "memory extraction batch user messages are invalid"
            )
        placeholders = ",".join("?" * len(batch_user_message_ids))
        user_rows = conn.execute(
            f"SELECT id, role, processed FROM messages WHERE user_id = ? "
            f"AND id IN ({placeholders})",
            (user_id, *batch_user_message_ids),
        ).fetchall()
        if (
            len(user_rows) != len(batch_user_message_ids)
            or any(
                row["role"] != "user" or row["processed"]
                for row in user_rows
            )
        ):
            raise ValueError(
                "memory extraction batch contains ineligible user messages"
            )
        allowed_evidence = {int(row["id"]) for row in user_rows}
        existing_rows = [
            dict(row) for row in conn.execute(
                "SELECT id, content, source FROM memory_items "
                "WHERE user_id = ? ORDER BY importance DESC, updated_at DESC",
                (user_id,),
            ).fetchall()
        ]

        inserted_ids: list[int] = []
        for memory in memories:
            evidence = memory.get("evidence_message_ids")
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(
                    isinstance(message_id, bool)
                    or not isinstance(message_id, int)
                    or message_id not in allowed_evidence
                    for message_id in evidence
                )
            ):
                raise ValueError(
                    "memory evidence must reference same-user batch user messages"
                )
            if _find_memory_duplicate_in_rows(memory["content"], existing_rows):
                continue
            item_id = insert_memory_item(
                user_id,
                content=memory["content"],
                importance=memory["importance"],
                source="lite_extracted",
                embedding=memory.get("embedding"),
                evidence_message_ids=evidence,
                conn=conn,
            )
            inserted_ids.append(item_id)
            existing_rows.append({
                "id": item_id,
                "content": memory["content"],
                "source": "lite_extracted",
            })

        conn.execute(
            "UPDATE memory_extraction_state SET "
            "last_processed_message_id = ?, last_success_at = ?, "
            "last_error = NULL, updated_at = ? WHERE user_id = ?",
            (through_message_id, now, now, user_id),
        )
        conn.commit()
        return inserted_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_memory_item(user_id: int, content: str,
                     importance: int = 1, source: str = "extracted",
                     embedding: bytes | None = None,
                     evidence_message_ids: list[int] | tuple[int, ...] | None = None) -> int:
    """Save a memory item with on-insert smart dedup.

    Dedup priority:
      1. Exact/text similarity (normalized, SequenceMatcher)
      2. Vector cosine similarity (if embedding provided)
    If a match is found: UPDATE (keep longer content, bump importance/access).
    Otherwise: INSERT new row.
    """
    from mochi.memory_contract import (
        decode_evidence_message_ids,
        encode_evidence_message_ids,
        merge_evidence_message_ids,
    )

    now = datetime.now(TZ).isoformat()
    conn = _connect()
    new_evidence = tuple(evidence_message_ids or ())
    norm_content = _normalize_text(content)
    candidates = conn.execute(
        "SELECT id, content, access_count, embedding, evidence_message_ids "
        "FROM memory_items WHERE user_id = ? "
        "ORDER BY updated_at DESC LIMIT 120",
        (user_id,),
    ).fetchall()
    existing = None
    for candidate in candidates:
        normalized = _normalize_text(candidate["content"])
        if not norm_content or not normalized:
            continue
        if (
            norm_content == normalized
            or difflib.SequenceMatcher(
                None, norm_content, normalized,
            ).ratio() >= 0.92
        ):
            existing = candidate
            break
    if not existing and embedding:
        for candidate in candidates:
            candidate_embedding = candidate["embedding"]
            if (
                candidate_embedding
                and _cosine_similarity(embedding, candidate_embedding) >= 0.92
            ):
                existing = candidate
                break

    if existing:
        merged_evidence = merge_evidence_message_ids(
            decode_evidence_message_ids(existing["evidence_message_ids"]),
            new_evidence,
        )
        evidence_json = encode_evidence_message_ids(merged_evidence)
        # Skip if content is identical
        if existing["content"] == content:
            conn.execute(
                "UPDATE memory_items SET evidence_message_ids = ? WHERE id = ?",
                (evidence_json, existing["id"]),
            )
            conn.commit()
            conn.close()
            return existing["id"]

        # Decide what to keep
        keep_content = (
            content
            if len(content) >= len(existing["content"])
            else existing["content"]
        )
        keep_emb = (
            embedding
            if len(content) >= len(existing["content"])
            else existing["embedding"]
        )

        if keep_emb is not None:
            conn.execute(
                "UPDATE memory_items SET content = ?, importance = MAX(importance, ?), "
                "updated_at = ?, access_count = access_count + 1, embedding = ?, "
                "evidence_message_ids = ? WHERE id = ?",
                (
                    keep_content, importance, now, keep_emb,
                    evidence_json, existing["id"],
                ),
            )
        else:
            conn.execute(
                "UPDATE memory_items SET content = ?, importance = MAX(importance, ?), "
                "updated_at = ?, access_count = access_count + 1, "
                "evidence_message_ids = ? WHERE id = ?",
                (keep_content, importance, now, evidence_json, existing["id"]),
            )
        item_id = existing["id"]
        if keep_content != existing["content"]:
            _invalidate_memory_kg_indexes(conn, [item_id])
        # Update FTS + vec indices (same conn — not yet committed)
        _sync_memory_item_indexes(conn, item_id, keep_content, keep_emb)
    else:
        evidence_json = encode_evidence_message_ids(new_evidence)
        cur = conn.execute(
            "INSERT INTO memory_items (user_id, category, content, importance, "
            "source, created_at, updated_at, embedding, evidence_message_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, "", content, importance, source, now, now,
                embedding, evidence_json,
            ),
        )
        item_id = cur.lastrowid
        _sync_memory_item_indexes(conn, item_id, content, embedding)

    conn.commit()
    conn.close()
    return item_id


def _memory_evidence_dates(
    conn: sqlite3.Connection,
    user_id: int,
    rows: list[sqlite3.Row] | list[dict],
) -> dict[int, dict[str, str]]:
    """Return the first and last user-evidence dates for supplied Memory Items."""
    from mochi.memory_contract import decode_evidence_message_ids

    evidence_by_item: dict[int, tuple[int, ...]] = {}
    message_ids: list[int] = []
    for row in rows:
        try:
            evidence = decode_evidence_message_ids(row["evidence_message_ids"])
        except ValueError:
            evidence = ()
        evidence_by_item[int(row["id"])] = evidence
        for message_id in evidence:
            if message_id not in message_ids:
                message_ids.append(message_id)
    if not message_ids:
        return {}

    placeholders = ",".join("?" * len(message_ids))
    messages = conn.execute(
        f"SELECT id, created_at FROM messages WHERE user_id = ? "
        f"AND role = 'user' AND id IN ({placeholders})",
        [user_id, *message_ids],
    ).fetchall()
    dates_by_message = {
        int(row["id"]): str(row["created_at"] or "")[:10]
        for row in messages
        if row["created_at"]
    }
    result: dict[int, dict[str, str]] = {}
    for item_id, evidence in evidence_by_item.items():
        dates = sorted({
            dates_by_message[message_id]
            for message_id in evidence
            if message_id in dates_by_message
        })
        if dates:
            result[item_id] = {
                "evidence_start": dates[0],
                "evidence_end": dates[-1],
            }
    return result


def get_memory_evidence_dates(
    user_id: int,
    item_ids: list[int],
) -> dict[int, dict[str, str]]:
    """Load evidence dates for Memory Items without exposing source messages."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT id, evidence_message_ids FROM memory_items "
            f"WHERE user_id = ? AND id IN ({placeholders})",
            [user_id, *item_ids],
        ).fetchall()
        return _memory_evidence_dates(conn, user_id, rows)
    finally:
        conn.close()


def recall_memory(user_id: int, query: str = "", limit: int = 20,
                  query_embedding: bytes | None = None,
                  bump_access: bool = True) -> list[dict]:
    """Recall by authoritative text signals with optional vector enhancement."""
    conn = _connect()
    vec_ok = _load_vec_conn(conn) if query_embedding else False
    now = datetime.now(TZ)
    limit = max(1, int(limit))

    vec_scores: dict[int, float] = {}
    bm25_scores: dict[int, float] = {}
    like_ids: set[int] = set()
    fallback_ids: set[int] = set()
    query_tokens = _fts_query_tokens(query)

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

    python_vector_ids: set[int] = set()
    if query_embedding and not vec_scores:
        rows = conn.execute(
            "SELECT id FROM memory_items "
            "WHERE user_id = ? AND embedding IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, RECALL_FALLBACK_LIMIT),
        ).fetchall()
        python_vector_ids.update(row["id"] for row in rows)

    if _FTS_AVAILABLE and query:
        try:
            if query_tokens:
                fts_query = " OR ".join(
                    f'"{token}"' for token in query_tokens
                )
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

    candidate_ids = (
        set(vec_scores)
        | python_vector_ids
        | set(bm25_scores)
    )

    if query:
        conditions = ["user_id = ?"]
        params: list = [user_id]
        like_terms = query_tokens[:12] or [query.strip()]
        like_terms = [term for term in like_terms if term]
        if like_terms:
            conditions.append(
                "(" + " OR ".join("content LIKE ?" for _ in like_terms) + ")"
            )
            params.extend(f"%{term}%" for term in like_terms)
        where = " AND ".join(conditions)
        params.append(RECALL_FALLBACK_LIMIT)
        fb_rows = conn.execute(
            f"SELECT id FROM memory_items WHERE {where} "
            f"ORDER BY importance DESC, updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        for r in fb_rows:
            candidate_ids.add(r["id"])
            like_ids.add(r["id"])

    if not query and len(candidate_ids) < limit:
        conditions = ["user_id = ?"]
        params = [user_id]
        params.append(RECALL_FALLBACK_LIMIT)
        rows = conn.execute(
            "SELECT id FROM memory_items WHERE " + " AND ".join(conditions)
            + " ORDER BY importance DESC, updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        for row in rows:
            candidate_ids.add(row["id"])
            fallback_ids.add(row["id"])

    if not candidate_ids:
        conn.close()
        return []

    id_ph = ",".join("?" * len(candidate_ids))
    id_list = list(candidate_ids)

    fetch_conditions = [f"id IN ({id_ph})", "user_id = ?"]
    fetch_params: list = id_list + [user_id]

    rows = conn.execute(
        "SELECT id, content, importance, access_count, source, "
        "last_accessed, embedding, evidence_message_ids, created_at, updated_at "
        f"FROM memory_items WHERE {' AND '.join(fetch_conditions)}",
        fetch_params,
    ).fetchall()
    evidence_dates = _memory_evidence_dates(conn, user_id, rows)

    scored: list[dict] = []
    half_life = RECALL_DECAY_HALF_LIFE_DAYS or 30.0

    for r in rows:
        rid = r["id"]

        try:
            updated = datetime.fromisoformat(r["updated_at"])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=TZ)
            days_ago = max((now - updated).total_seconds() / 86400, 0)
        except (ValueError, TypeError):
            days_ago = 365
        decay = math.exp(-math.log(2) * days_ago / half_life)

        access_bonus = min((r["access_count"] or 0) * 0.5, 3)
        base_score = (r["importance"] * 2 + access_bonus) * decay

        vec_sim = 0.0
        has_vector = bool(query_embedding and r["embedding"])
        if rid in vec_scores:
            vec_sim = max(1.0 - vec_scores[rid], 0.0)
        elif has_vector:
            vec_sim = _cosine_similarity(query_embedding, r["embedding"])

        bm25_norm = 0.0
        if rid in bm25_scores:
            bm25_norm = min(1.0, abs(bm25_scores[rid]) / 10.0)

        fts_hit = (
            rid in bm25_scores
            and _fts_match_is_relevant(query_tokens, r["content"])
        )
        like_hit = rid in like_ids and (
            not query_tokens
            or _fts_match_is_relevant(query_tokens, r["content"])
        )
        vector_hit = has_vector and vec_sim >= RECALL_VEC_SIM_THRESHOLD
        if fts_hit or like_hit:
            match_source = "hybrid" if vector_hit else (
                "fts" if fts_hit else "like"
            )
        elif vector_hit:
            match_source = "vector"
        elif rid in fallback_ids:
            match_source = "fallback"
        else:
            continue

        score = (
            RECALL_VEC_SIM_WEIGHT * vec_sim
            + RECALL_BM25_WEIGHT * bm25_norm
            + base_score
            + (RECALL_KEYWORD_BOOST if bm25_norm > 0 else 0)
        )

        scored.append({
            "id": rid,
            "content": r["content"],
            "importance": r["importance"],
            "source": r["source"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            **evidence_dates.get(rid, {
                "evidence_start": "",
                "evidence_end": "",
            }),
            "score": round(score, 3),
            "vec_sim": round(vec_sim, 3),
            "match_source": match_source,
            "fts_hit": fts_hit,
            "has_vector": has_vector,
        })

    scored.sort(
        key=lambda item: (
            item["match_source"] != "fallback",
            item["score"],
        ),
        reverse=True,
    )
    result = scored[:limit]

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


def get_memory_extraction_references(
    user_id: int, limit: int = 80,
) -> list[dict]:
    """Return a bounded important/recent duplicate-reference set."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content, importance FROM memory_items "
        "WHERE user_id = ? ORDER BY importance DESC, updated_at DESC LIMIT ?",
        (user_id, max(1, min(int(limit), 120))),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_memory_items_by_ids(user_id: int, item_ids: list[int]) -> list[dict]:
    """Load authoritative Memory Items in caller-provided order."""
    from mochi.memory_contract import decode_evidence_message_ids

    if not item_ids:
        return []
    placeholders = ",".join("?" * len(item_ids))
    conn = _connect()
    rows = conn.execute(
        f"SELECT id, content, importance, source, embedding, "
        f"evidence_message_ids, created_at, updated_at FROM memory_items "
        f"WHERE user_id = ? AND id IN ({placeholders})",
        [user_id, *item_ids],
    ).fetchall()
    conn.close()
    by_id = {}
    for row in rows:
        item = dict(row)
        item["evidence_message_ids"] = list(
            decode_evidence_message_ids(item["evidence_message_ids"])
        )
        by_id[row["id"]] = item
    return [by_id[item_id] for item_id in item_ids if item_id in by_id]


def get_memory_evidence_receipt(
    user_id: int,
    item_id: int,
    *,
    max_message_chars: int = 2000,
) -> dict | None:
    """Load one owner's recorded source messages for lazy admin display."""
    from mochi.memory_contract import decode_evidence_message_ids

    max_chars = max(100, min(int(max_message_chars), 5000))
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, content, importance, source, evidence_message_ids, "
            "created_at, updated_at FROM memory_items "
            "WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if row is None:
            return None

        message_ids = decode_evidence_message_ids(row["evidence_message_ids"])
        messages_by_id: dict[int, dict] = {}
        if message_ids:
            placeholders = ",".join("?" * len(message_ids))
            messages = conn.execute(
                f"SELECT id, content, created_at FROM messages "
                f"WHERE user_id = ? AND role = 'user' "
                f"AND id IN ({placeholders})",
                (user_id, *message_ids),
            ).fetchall()
            messages_by_id = {
                int(message["id"]): dict(message) for message in messages
            }

        source_messages: list[dict] = []
        for message_id in message_ids:
            message = messages_by_id.get(message_id)
            if message is None:
                source_messages.append({
                    "message_id": message_id,
                    "available": False,
                })
                continue
            content = str(message["content"] or "")
            source_messages.append({
                "message_id": message_id,
                "available": True,
                "created_at": str(message["created_at"] or ""),
                "content": content[:max_chars],
                "truncated": len(content) > max_chars,
            })

        return {
            "item": {
                "id": row["id"],
                "content": row["content"],
                "importance": row["importance"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "source_status": "recorded" if message_ids else "not_recorded",
            "source_messages": source_messages,
        }
    finally:
        conn.close()


def delete_memory_items(ids: list[int], deleted_by: str = "system") -> int:
    """Soft-delete memory items: copy to trash, clean indexes, then delete."""
    if not ids:
        return 0
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    placeholders = ",".join("?" * len(ids))
    try:
        conn.execute("BEGIN IMMEDIATE")
        items = conn.execute(
            f"SELECT id, user_id, category, content, importance, source, "
            f"evidence_message_ids, created_at "
            f"FROM memory_items WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        existing_ids = [item["id"] for item in items]
        for item in items:
            _insert_memory_trash_snapshot(
                conn,
                item,
                deleted_by=deleted_by,
                deleted_at=now,
            )
        _invalidate_memory_kg_indexes(conn, existing_ids)
        _delete_memory_item_indexes(conn, existing_ids)
        cursor = conn.execute(
            f"DELETE FROM memory_items WHERE id IN ({placeholders})", ids,
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def merge_memory_items(keep_id: int, delete_ids: list[int],
                       merged_content: str, new_importance: int | None = None) -> None:
    if keep_id in delete_ids:
        raise ValueError("keep_id cannot also be deleted")
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        keep_row = conn.execute(
            "SELECT id, user_id, category, content, importance, source, "
            "evidence_message_ids, created_at FROM memory_items WHERE id = ?",
            (keep_id,),
        ).fetchone()
        if keep_row is None:
            raise ValueError(f"Memory Item {keep_id} not found")
        _insert_memory_trash_snapshot(
            conn, keep_row, deleted_by="merge_keep", deleted_at=now,
        )
        if new_importance is not None:
            conn.execute(
                "UPDATE memory_items SET content = ?, importance = ?, "
                "updated_at = ? WHERE id = ?",
                (merged_content, new_importance, now, keep_id),
            )
        else:
            conn.execute(
                "UPDATE memory_items SET content = ?, updated_at = ? WHERE id = ?",
                (merged_content, now, keep_id),
            )
        if delete_ids:
            placeholders = ",".join("?" * len(delete_ids))
            items = conn.execute(
                f"SELECT id, user_id, category, content, importance, source, "
                f"evidence_message_ids, created_at "
                f"FROM memory_items WHERE id IN ({placeholders})",
                delete_ids,
            ).fetchall()
            existing_ids = [item["id"] for item in items]
            for item in items:
                _insert_memory_trash_snapshot(
                    conn, item, deleted_by="dedup", deleted_at=now,
                )
            _invalidate_memory_kg_indexes(conn, existing_ids)
            _delete_memory_item_indexes(conn, existing_ids)
            conn.execute(
                f"DELETE FROM memory_items WHERE id IN ({placeholders})",
                delete_ids,
            )
        _invalidate_memory_kg_indexes(conn, [keep_id])
        _sync_memory_item_indexes(conn, keep_id, merged_content, None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_all_memories(user_id: int, limit: int = 50) -> list[dict]:
    """List recent Memory Items with their user-evidence dates."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, content, importance, source, evidence_message_ids, "
        "created_at, updated_at FROM memory_items WHERE user_id = ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    evidence_dates = _memory_evidence_dates(conn, user_id, rows)
    conn.close()
    return [
        {
            **{
                key: value for key, value in dict(row).items()
                if key != "evidence_message_ids"
            },
            **evidence_dates.get(row["id"], {
                "evidence_start": "",
                "evidence_end": "",
            }),
        }
        for row in rows
    ]


def get_memory_stats(user_id: int) -> dict:
    """Get memory system statistics."""
    conn = _connect()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM memory_items WHERE user_id = ?", (user_id,)
    ).fetchone()["cnt"]
    high_imp = conn.execute(
        "SELECT COUNT(*) as cnt FROM memory_items "
        "WHERE user_id = ? AND importance >= 3", (user_id,)
    ).fetchone()["cnt"]
    conn.close()
    return {
        "total": total,
        "high_importance": high_imp,
    }


def list_memory_trash(user_id: int, limit: int = 20) -> list[dict]:
    """List recently deleted memories (trash bin)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, original_id, content, importance, deleted_by, deleted_at "
        "FROM memory_trash WHERE user_id = ? ORDER BY deleted_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def restore_memory_from_trash(trash_id: int, user_id: int) -> int | None:
    """Restore a memory from trash back to memory_items. Returns new item id or None."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute(
            "SELECT original_id, user_id, content, importance, source, "
            "evidence_message_ids, original_created "
            "FROM memory_trash WHERE id = ? AND user_id = ?",
            (trash_id, user_id),
        ).fetchone()
        if not item:
            conn.rollback()
            return None
        cursor = conn.execute(
            "INSERT INTO memory_items "
            "(user_id, category, content, importance, access_count, source, "
            "evidence_message_ids, created_at, updated_at, last_accessed) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                item["user_id"], "", item["content"],
                item["importance"], item["source"],
                item["evidence_message_ids"], item["original_created"], now, now,
            ),
        )
        new_id = cursor.lastrowid
        _sync_memory_item_indexes(conn, new_id, item["content"], None)
        conn.execute("DELETE FROM memory_trash WHERE id = ?", (trash_id,))
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_memory_item(
    item_id: int,
    user_id: int,
    *,
    content: str,
    importance: int,
    embedding: bytes | None = None,
) -> bool:
    """Update one item and every derived index in one transaction."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT content, embedding FROM memory_items "
            "WHERE id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if existing is None:
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE memory_items SET content = ?, importance = ?, "
            "updated_at = ?, embedding = ? WHERE id = ? AND user_id = ?",
            (
                content, importance, now, embedding,
                item_id, user_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False
        if existing["content"] != content or existing["embedding"] != embedding:
            _invalidate_memory_kg_indexes(conn, [item_id])
        _sync_memory_item_indexes(conn, item_id, content, embedding)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


# ═══════════════════════════════════════════════════════════════════════════
# Legacy Core import (Layer 1)
# ═══════════════════════════════════════════════════════════════════════════

def get_core_memory(user_id: int) -> str:
    """Read a pre-file-store Core only for one-time canonical migration."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT content FROM core_memory WHERE user_id = ?", (user_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    return row["content"] if row else ""

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
              cost_usd: float | None = None,
              reasoning_tokens: int | None = None,
              cached_prompt_tokens: int | None = None) -> None:
    now = datetime.now(TZ).isoformat()
    eff_call_type = call_type or purpose
    conn = _connect()
    conn.execute(
        """INSERT INTO usage_log (prompt_tokens, completion_tokens, total_tokens,
           tool_calls, model, purpose, created_at,
           tool_name, model_role, call_type, usage_stage,
           prompt_system_tokens, prompt_history_tokens, prompt_tool_tokens, cost_usd,
           reasoning_tokens, cached_prompt_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (prompt_tokens, completion_tokens, total_tokens, tool_calls, model, purpose, now,
         tool_name, model_role, eff_call_type, usage_stage,
         prompt_system_tokens, prompt_history_tokens, prompt_tool_tokens, cost_usd,
         reasoning_tokens, cached_prompt_tokens),
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
                      COALESCE(SUM(completion_tokens), 0) as c,
                      COALESCE(SUM(reasoning_tokens), 0) as r
               FROM usage_log WHERE created_at >= ? GROUP BY model""",
            (since,),
        ).fetchall():
            result[r["model"] or "unknown"] = {
                "prompt": r["p"], "completion": r["c"], "reasoning": r["r"],
            }
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


# ═══════════════════════════════════════════════════════════════════════════
# Proactive Log — delivered autonomous text audit
# ═══════════════════════════════════════════════════════════════════════════

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


def cleanup_heartbeat_log(days: int = 30) -> int:
    """Delete heartbeat log entries older than the retention window."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM heartbeat_log WHERE created_at < ?",
        (cutoff,),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def get_last_user_message_time(user_id: int) -> str | None:
    conn = _connect()
    row = conn.execute(
        "SELECT created_at FROM messages WHERE user_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["created_at"] if row else None


def get_last_user_message(user_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT content, created_at FROM messages "
        "WHERE user_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_daily_message_counts(user_id: int, days: int = 7) -> list[dict]:
    """Get per-day user message counts for the last N days.

    Returns: [{"date": "2026-02-22", "count": 15}, ...] ordered oldest→newest.
    Always returns exactly `days` entries (count=0 for silent days).
    """
    # wall-clock 故意：activity_pattern observer 给 LLM 的物理对话趋势图
    now = datetime.now(TZ)
    # wall-clock 故意：物理日历日范围
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = _connect()
    rows = conn.execute(
        "SELECT DATE(created_at) as day, COUNT(*) as cnt "
        "FROM messages WHERE user_id = ? AND role = 'user' AND created_at >= ? "
        "GROUP BY DATE(created_at) ORDER BY day",
        (user_id, start),
    ).fetchall()
    conn.close()

    counts_map = {r["day"]: r["cnt"] for r in rows}
    result = []
    for i in range(days):
        # wall-clock 故意：物理日历日 chart key
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


# ── Skill mode (skilloff / skillon) ──────────────────────────────

def get_skill_mode() -> str:
    """Return current skill mode: ``"on"`` (default) or ``"off"``."""
    conn = _connect()
    row = conn.execute(
        "SELECT value FROM skill_config WHERE skill_name = '_system' AND key = 'skill_mode'",
    ).fetchone()
    conn.close()
    return row[0] if row else "on"


def set_skill_mode(mode: str) -> None:
    """Set skill mode.  ``"off"`` persists; anything else clears the row (= on)."""
    conn = _connect()
    if mode == "off":
        now = datetime.now(TZ).isoformat()
        conn.execute(
            "INSERT INTO skill_config (skill_name, key, value, updated_at) "
            "VALUES ('_system', 'skill_mode', 'off', ?) "
            "ON CONFLICT(skill_name, key) DO UPDATE SET value = 'off', updated_at = ?",
            (now, now),
        )
    else:
        conn.execute(
            "DELETE FROM skill_config WHERE skill_name = '_system' AND key = 'skill_mode'",
        )
    conn.commit()
    conn.close()


_SCHEDULED_RUN_ERROR_MAX_CHARS = 500
_SCHEDULED_RUN_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password)(\s*[=:]\s*)\S+"
)


def _safe_scheduled_run_error(error: str) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ")
    return _SCHEDULED_RUN_SECRET_RE.sub(r"\1\2[REDACTED]", text)[
        :_SCHEDULED_RUN_ERROR_MAX_CHARS
    ]


def recover_interrupted_scheduled_runs() -> int:
    """Recover scheduler-owned jobs once during bot process startup."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    cursor = conn.execute(
        "UPDATE scheduled_runs SET status = 'failed', finished_at = ?, "
        "error = 'Interrupted by process restart' WHERE status = 'running'",
        (now,),
    )
    recovered = cursor.rowcount
    conn.commit()
    conn.close()
    if recovered:
        logger.warning("Recovered %d interrupted scheduled run(s)", recovered)
    return recovered


def claim_scheduled_run(job_name: str, period_key: str) -> bool:
    """Atomically claim a missing or failed scheduled period."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM scheduled_runs "
            "WHERE job_name = ? AND period_key = ?",
            (job_name, period_key),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO scheduled_runs "
                "(job_name, period_key, status, attempt_count, started_at) "
                "VALUES (?, ?, 'running', 1, ?)",
                (job_name, period_key, now),
            )
            conn.commit()
            return True
        if row["status"] != "failed":
            conn.commit()
            return False
        conn.execute(
            "UPDATE scheduled_runs SET status = 'running', "
            "attempt_count = attempt_count + 1, started_at = ?, "
            "finished_at = NULL, error = '' "
            "WHERE job_name = ? AND period_key = ? AND status = 'failed'",
            (now, job_name, period_key),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_scheduled_run(
    job_name: str,
    period_key: str,
    *,
    success: bool,
    error: str = "",
) -> None:
    """Finish a currently claimed scheduled period."""
    now = datetime.now(TZ).isoformat()
    status = "success" if success else "failed"
    safe_error = "" if success else _safe_scheduled_run_error(error)
    conn = _connect()
    cursor = conn.execute(
        "UPDATE scheduled_runs SET status = ?, finished_at = ?, error = ? "
        "WHERE job_name = ? AND period_key = ? AND status = 'running'",
        (status, now, safe_error, job_name, period_key),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        conn.close()
        raise RuntimeError(
            f"Scheduled run {job_name}/{period_key} is not currently claimed"
        )
    conn.commit()
    conn.close()


def get_scheduled_run(job_name: str, period_key: str) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT job_name, period_key, status, attempt_count, started_at, "
        "finished_at, error FROM scheduled_runs "
        "WHERE job_name = ? AND period_key = ?",
        (job_name, period_key),
    ).fetchone()
    conn.close()
    return dict(row) if row else None
