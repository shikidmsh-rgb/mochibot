"""Knowledge Graph: entity-relationship triples with temporal validity.

Stores structured facts about entities (people, pets, places, concepts)
and their relationships as subject-predicate-object triples.
Temporal validity (valid_from/valid_to) tracks when facts become/cease to be true.

Primary consumers: memory_engine.py (extraction), ai_client.py (query-time injection)
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta

from mochi.db import _connect
from mochi.config import TZ

log = logging.getLogger(__name__)

# Predicates where only one object can be valid at a time per subject.
# When a new triple is added with a single-valued predicate, any existing
# active triple with the same (subject, predicate) is auto-invalidated.
SINGLE_VALUED_PREDICATES = frozenset({
    "is_a", "has_breed", "has_gender", "has_status",
    "born_in", "adopted_in", "weighs", "is_neutered",
})

# Emoji pattern: common animal/object emoji + supplementary plane symbols
_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
    r"\U0000200D\U00002702-\U000027B0\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]+",
)


def _normalize_name(name: str) -> str:
    """Normalize entity name for canonical storage and matching.

    Strips emoji, normalizes unicode, lowercases, collapses whitespace.
    """
    name = unicodedata.normalize("NFKC", name)
    name = _EMOJI_RE.sub("", name)
    name = re.sub(r"[()（）]", "", name)
    name = re.sub(r"\s+", " ", name).strip().lower()
    return name


# ── Entity CRUD ───────────────────────────────────────────────────────


def get_or_create_entity(
    user_id: int, name: str, entity_type: str = "concept",
    display_name: str | None = None,
) -> int:
    """Get or create an entity. Returns entity id.

    Name is normalized (lowercase, emoji-stripped) for canonical matching.
    UNIQUE(user_id, name) constraint ensures idempotency.
    """
    canonical = _normalize_name(name)
    if not canonical:
        raise ValueError(f"Empty entity name after normalization: {name!r}")
    disp = (display_name or name).strip()
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities "
            "(user_id, name, display_name, entity_type, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, canonical, disp, entity_type, now),
        )
        row = conn.execute(
            "SELECT id, display_name FROM kg_entities "
            "WHERE user_id = ? AND name = ?",
            (user_id, canonical),
        ).fetchone()
        if row and display_name and len(disp) > len(row["display_name"]):
            conn.execute(
                "UPDATE kg_entities SET display_name = ? WHERE id = ?",
                (disp, row["id"]),
            )
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def get_entity_by_name(user_id: int, name: str) -> dict | None:
    """Lookup entity by normalized name. Returns dict or None."""
    canonical = _normalize_name(name)
    if not canonical:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, user_id, name, display_name, entity_type, created_at "
            "FROM kg_entities WHERE user_id = ? AND name = ?",
            (user_id, canonical),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_entities(user_id: int, entity_type: str | None = None) -> list[dict]:
    """List all entities, optionally filtered by type."""
    conn = _connect()
    try:
        if entity_type:
            rows = conn.execute(
                "SELECT id, name, display_name, entity_type "
                "FROM kg_entities WHERE user_id = ? AND entity_type = ? "
                "ORDER BY name",
                (user_id, entity_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, display_name, entity_type "
                "FROM kg_entities WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Triple CRUD ───────────────────────────────────────────────────────


def add_triple(
    user_id: int, subject_id: int, predicate: str, object_id: int,
    valid_from: str | None = None, valid_to: str | None = None,
    source: str = "chat", confidence: float = 1.0,
) -> int:
    """Add a relationship triple. Returns triple id.

    Idempotent: if an identical active triple exists, returns its id.
    For single-valued predicates, auto-invalidates existing active triple
    with same (subject, predicate) if the object differs.
    """
    predicate = predicate.strip().lower()
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM kg_triples "
            "WHERE user_id = ? AND subject_id = ? AND predicate = ? "
            "AND object_id = ? AND valid_to IS NULL LIMIT 1",
            (user_id, subject_id, predicate, object_id),
        ).fetchone()
        if existing:
            return existing["id"]

        if predicate in SINGLE_VALUED_PREDICATES:
            conn.execute(
                "UPDATE kg_triples SET valid_to = ? "
                "WHERE user_id = ? AND subject_id = ? AND predicate = ? "
                "AND valid_to IS NULL",
                (now, user_id, subject_id, predicate),
            )

        cur = conn.execute(
            "INSERT INTO kg_triples "
            "(user_id, subject_id, predicate, object_id, "
            " valid_from, valid_to, source, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, subject_id, predicate, object_id,
             valid_from, valid_to, source, confidence, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def invalidate_triple(triple_id: int, ended_date: str | None = None) -> bool:
    """Mark a triple as no longer valid (set valid_to). Returns True if updated."""
    ended = ended_date or datetime.now(TZ).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE kg_triples SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
            (ended, triple_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Query ─────────────────────────────────────────────────────────────


def query_entity(
    user_id: int, name: str, as_of: str | None = None,
    limit: int | None = None,
) -> dict | None:
    """Get all relationships for an entity.

    Returns:
        {"entity": {...}, "as_subject": [...], "as_object": [...]}
        or None if entity not found.
    """
    from mochi.config import KG_MAX_TRIPLES_PER_ENTITY
    limit = limit or KG_MAX_TRIPLES_PER_ENTITY

    entity = get_entity_by_name(user_id, name)
    if not entity:
        return None

    eid = entity["id"]
    conn = _connect()
    try:
        if as_of:
            time_filter = (
                "AND (t.valid_from IS NULL OR t.valid_from <= ?) "
                "AND (t.valid_to IS NULL OR t.valid_to >= ?)"
            )
            time_params = (as_of, as_of)
        else:
            time_filter = "AND t.valid_to IS NULL"
            time_params = ()

        as_subject = conn.execute(
            f"SELECT t.id, t.predicate, t.valid_from, t.valid_to, t.confidence, "
            f"  e.name AS object_name, e.display_name AS object_display, "
            f"  e.entity_type AS object_type "
            f"FROM kg_triples t "
            f"JOIN kg_entities e ON e.id = t.object_id "
            f"WHERE t.subject_id = ? {time_filter} "
            f"ORDER BY t.created_at DESC LIMIT ?",
            (eid, *time_params, limit),
        ).fetchall()

        as_object = conn.execute(
            f"SELECT t.id, t.predicate, t.valid_from, t.valid_to, t.confidence, "
            f"  e.name AS subject_name, e.display_name AS subject_display, "
            f"  e.entity_type AS subject_type "
            f"FROM kg_triples t "
            f"JOIN kg_entities e ON e.id = t.subject_id "
            f"WHERE t.object_id = ? {time_filter} "
            f"ORDER BY t.created_at DESC LIMIT ?",
            (eid, *time_params, limit),
        ).fetchall()

        return {
            "entity": entity,
            "as_subject": [dict(r) for r in as_subject],
            "as_object": [dict(r) for r in as_object],
        }
    finally:
        conn.close()


def entity_context_for_prompt(user_id: int, entity_name: str) -> str:
    """Format entity relationships as compact text for prompt injection.

    Returns empty string if no data found or entity unknown.
    Token-limited by KG_MAX_ENTITY_CONTEXT_TOKENS config.
    """
    from mochi.config import KG_MAX_ENTITY_CONTEXT_TOKENS

    result = query_entity(user_id, entity_name)
    if not result:
        return ""

    entity = result["entity"]
    pred_groups: dict[str, list[str]] = {}

    for tri in result["as_subject"]:
        pred = tri["predicate"]
        disp = tri.get("object_display") or tri.get("object_name", "?")
        pred_groups.setdefault(pred, []).append(disp)

    for tri in result["as_object"]:
        pred = tri["predicate"]
        disp = tri.get("subject_display") or tri.get("subject_name", "?")
        pred_groups.setdefault(f"\u2190{pred}", []).append(disp)

    if not pred_groups:
        return ""

    _PRED_PRIORITY = [
        "has_condition", "has_status", "is_a", "has_breed", "has_gender",
        "weighs", "born_in", "has_personality", "is_neutered", "adopted_in",
    ]
    ordered_preds: list[str] = []
    for p in _PRED_PRIORITY:
        if p in pred_groups:
            ordered_preds.append(p)
    for p in pred_groups:
        if p not in ordered_preds:
            ordered_preds.append(p)

    parts: list[str] = []
    for pred in ordered_preds:
        parts.append(f"{pred}:{','.join(pred_groups[pred])}")

    type_label = entity.get("entity_type", "")
    disp = entity.get("display_name", entity.get("name", "?"))
    text = f"\u3010{disp}\u3011({type_label}) " + " | ".join(parts)

    max_chars = KG_MAX_ENTITY_CONTEXT_TOKENS * 2 // 3
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."

    return text


def find_matching_entities(
    user_id: int, text: str,
    matchable_types: tuple[str, ...] = ("person", "pet"),
) -> list[str]:
    """Find known entity names that appear in text.

    Only matches entities of matchable_types to reduce false positives
    (e.g., common words that happen to be entity names).
    """
    from mochi.config import KG_ENTITY_MATCH_MIN_LENGTH
    min_len = max(1, KG_ENTITY_MATCH_MIN_LENGTH)

    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in matchable_types)
        rows = conn.execute(
            f"SELECT name, display_name FROM kg_entities "
            f"WHERE user_id = ? AND entity_type IN ({placeholders}) "
            f"AND LENGTH(name) >= ?",
            (user_id, *matchable_types, min_len),
        ).fetchall()
    finally:
        conn.close()

    text_lower = text.lower()
    matched = []
    for row in rows:
        if row["name"] in text_lower:
            matched.append(row["name"])
    return matched


# ── Stats & Cleanup ──────────────────────────────────────────────────


def get_kg_stats(user_id: int) -> dict:
    """Return KG statistics for diagnostics."""
    conn = _connect()
    try:
        entities = conn.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        active_triples = conn.execute(
            "SELECT COUNT(*) FROM kg_triples "
            "WHERE user_id = ? AND valid_to IS NULL",
            (user_id,),
        ).fetchone()[0]
        total_triples = conn.execute(
            "SELECT COUNT(*) FROM kg_triples WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        return {
            "entities": entities,
            "active_triples": active_triples,
            "total_triples": total_triples,
        }
    finally:
        conn.close()


def cleanup_expired_triples(days: int = 90) -> int:
    """Hard-delete triples whose valid_to is older than N days. Returns count purged."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM kg_triples WHERE valid_to IS NOT NULL AND valid_to < ?",
            (cutoff,),
        )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()
