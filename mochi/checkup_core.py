"""Checkup core — lightweight system health report.

Aggregates diagnostics from multiple framework modules (memory_engine,
prompt_loader, knowledge_graph, error_buffer, runtime_state, db).
Lives at engine level because skills cannot import these modules directly.

Both the checkup skill handler and admin API call run_checkup().
"""

import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

# Identity prompts that are always injected into the system prompt
_IDENTITY_PROMPTS = [
    "system_chat/soul",
    "system_chat/user",
    "system_chat/agent",
    "system_chat/runtime_context",
]


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken. Falls back to chars÷4 estimate."""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model("gpt-4o")
        return len(enc.encode(text))
    except Exception:
        return len(text) // 4


def _check_prompt_size(user_id: int) -> dict:
    """Check identity prompt sizes and core memory token usage."""
    from mochi.prompt_loader import get_prompt
    from mochi.memory_engine import audit_core_memory_tokens

    identity = {}
    total_tokens = 0
    for name in _IDENTITY_PROMPTS:
        content = get_prompt(name)
        chars = len(content)
        tokens = _count_tokens(content) if content else 0
        identity[name] = {"chars": chars, "tokens": tokens}
        total_tokens += tokens

    core_audit = audit_core_memory_tokens(user_id)

    return {
        "identity_prompts": identity,
        "identity_total_tokens": total_tokens,
        "core_memory": {
            "tokens": core_audit.get("tokens", 0),
            "max_tokens": _get_core_memory_max_tokens(),
            "over_budget": core_audit.get("over_budget", False),
        },
    }


def _get_core_memory_max_tokens() -> int:
    from mochi.config import CORE_MEMORY_MAX_TOKENS
    return CORE_MEMORY_MAX_TOKENS


def _check_database() -> dict:
    """Check database file size, integrity, and key table row counts."""
    from mochi.config import DB_PATH
    from mochi.db import _connect

    file_size = 0
    if DB_PATH.exists():
        file_size = os.path.getsize(DB_PATH)

    conn = _connect()
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]

        table_counts = {}
        for table in ("messages", "memory_items", "kg_entities", "kg_triples"):
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
                table_counts[table] = row[0]
            except Exception:
                table_counts[table] = -1
    finally:
        conn.close()

    return {
        "file_size_bytes": file_size,
        "integrity_ok": integrity == "ok",
        "table_counts": table_counts,
    }


def _check_memory(user_id: int) -> dict:
    """Check memory system stats: items, categories, KG, trash."""
    from mochi.db import get_memory_stats, _connect
    from mochi.knowledge_graph import get_kg_stats

    stats = get_memory_stats(user_id)
    kg = get_kg_stats(user_id)

    conn = _connect()
    try:
        trash_count = conn.execute(
            "SELECT COUNT(*) FROM memory_trash WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    except Exception:
        trash_count = 0
    finally:
        conn.close()

    return {
        "total": stats.get("total", 0),
        "categories": stats.get("categories", {}),
        "kg_entities": kg.get("entities", 0),
        "kg_active_triples": kg.get("active_triples", 0),
        "trash_count": trash_count,
    }


def _check_runtime() -> dict:
    """Check recent errors and last maintenance."""
    from mochi.error_buffer import get_recent_errors
    from mochi.runtime_state import get_maintenance_summary

    errors = get_recent_errors(hours=24)
    maintenance = get_maintenance_summary()

    return {
        "error_count_24h": len(errors),
        "last_maintenance": maintenance or None,
    }


def run_checkup(user_id: int = 0) -> dict:
    """Run a lightweight system health check.

    Each category is independently try/excepted so one failure
    does not block the others.

    Returns a dict with keys: prompt_size, database, memory, runtime, checked_at.
    """
    from mochi.config import OWNER_USER_ID
    uid = user_id or OWNER_USER_ID

    result = {}

    for key, fn, args in [
        ("prompt_size", _check_prompt_size, (uid,)),
        ("database", _check_database, ()),
        ("memory", _check_memory, (uid,)),
        ("runtime", _check_runtime, ()),
    ]:
        try:
            result[key] = fn(*args)
        except Exception as e:
            log.error("Checkup section '%s' failed: %s", key, e, exc_info=True)
            result[key] = {"error": str(e)}

    from mochi.config import TZ
    result["checked_at"] = datetime.now(TZ).isoformat(timespec="seconds")

    return result
