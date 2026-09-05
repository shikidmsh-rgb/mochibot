"""Text-authoritative recall and derived-index consistency."""

from __future__ import annotations

import struct

import pytest

from mochi.ai_client import _retrieve_memories_for_turn, _user_last_recall
from mochi.db import (
    _connect,
    delete_memory_items,
    list_memory_trash,
    merge_memory_items,
    recall_memory,
    restore_memory_from_trash,
    save_memory_item,
    save_message,
    update_memory_item,
)
from mochi.knowledge_graph import (
    RelationshipCurationConflict,
    RelationshipCurationError,
    curate_relationships,
    find_matching_entities,
    list_active_relationships,
)


class Pool:
    def __init__(self, embedding=None, error=None):
        self.embedding = embedding
        self.error = error
        self.queries = []

    def embed(self, text):
        self.queries.append(text)
        if self.error:
            raise self.error
        return self.embedding


@pytest.fixture(autouse=True)
def _recall_config(monkeypatch):
    import mochi.config as config

    _user_last_recall.clear()
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL", True)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_TOP_K", 5)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_ITEMS", 3)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MIN_VEC_SIM", 0.35)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_CHARS", 320)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_TOKENS", 600)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_COOLDOWN", 0)
    monkeypatch.setattr(config, "KG_ENABLED", False)


@pytest.mark.parametrize("cooldown", [0, 120])
def test_consecutive_topics_recall_unless_cooldown_is_explicit(
    monkeypatch, cooldown,
):
    import mochi.ai_client as ai_client
    import mochi.config as config
    import mochi.model_pool as model_pool

    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_COOLDOWN", cooldown)
    monkeypatch.setattr(ai_client.time, "time", lambda: 1000.0)
    pool = Pool()
    monkeypatch.setattr(model_pool, "get_pool", lambda: pool)
    save_memory_item(1, "Gets nervous before presentations", source="admin")
    save_memory_item(1, "Has a cat that dislikes car rides", source="admin")

    first = _retrieve_memories_for_turn("presentations", 1)
    second = _retrieve_memories_for_turn("cat", 1)

    assert [item["text"] for item in first] == [
        "Gets nervous before presentations",
    ]
    assert [item["text"] for item in second] == (
        ["Has a cat that dislikes car rides"] if cooldown == 0 else []
    )
    assert pool.queries == (
        ["presentations", "cat"] if cooldown == 0 else ["presentations"]
    )


@pytest.mark.parametrize(
    "pool",
    [Pool(), Pool(error=RuntimeError("provider offline"))],
)
def test_auto_recall_uses_text_when_embedding_is_unavailable(
    monkeypatch, pool,
):
    import mochi.model_pool as model_pool

    first_evidence_id = save_message(1, "user", "I like jasmine tea")
    last_evidence_id = save_message(1, "user", "Jasmine tea is still my favorite")
    conn = _connect()
    conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        ("2026-08-14T10:00:00+08:00", first_evidence_id),
    )
    conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        ("2026-08-15T10:00:00+08:00", last_evidence_id),
    )
    conn.commit()
    conn.close()
    save_memory_item(
        1, "喜欢茉莉花茶", source="admin",
        evidence_message_ids=[first_evidence_id, last_evidence_id],
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: pool)

    recalled = _retrieve_memories_for_turn("我喜欢什么花茶？", 1)

    assert [item["text"] for item in recalled] == ["喜欢茉莉花茶"]
    assert recalled[0]["evidence_start"] == "2026-08-14"
    assert recalled[0]["evidence_end"] == "2026-08-15"


def test_like_fallback_is_authoritative_without_fts_or_embedding(monkeypatch):
    import mochi.db as db
    import mochi.model_pool as model_pool

    save_memory_item(
        1, "Likes jasmine tea", source="admin",
    )
    monkeypatch.setattr(db, "_FTS_AVAILABLE", False)
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())

    recalled = _retrieve_memories_for_turn(
        "Do I like jasmine tea?", 1,
    )

    assert [item["text"] for item in recalled] == [
        "Likes jasmine tea",
    ]
    assert recalled[0]["evidence_start"] == ""


def test_semantic_query_never_uses_recent_only_filler(monkeypatch):
    import mochi.model_pool as model_pool

    save_memory_item(
        1, "Unrelated but very recent detail", source="admin",
    )
    monkeypatch.setattr(model_pool, "get_pool", lambda: Pool())

    assert _retrieve_memories_for_turn("jasmine", 1) == []
    assert recall_memory(
        1, query="jasmine", bump_access=False,
    ) == []


def test_edit_delete_merge_restore_keep_fts_vector_and_kg_consistent(
    monkeypatch,
):
    import mochi.db as db

    embedding = struct.pack("1536f", 1.0, *([0.0] * 1535))
    other_embedding = struct.pack(
        "1536f", 0.0, 1.0, *([0.0] * 1534),
    )
    kept_id = save_memory_item(
        1, "Old alpha memory", source="admin",
        embedding=embedding,
    )
    deleted_id = save_memory_item(
        1, "Temporary beta memory", source="admin",
        embedding=other_embedding,
    )
    conn = _connect()
    subject = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'alpha', 'Alpha', 'person', 'now')"
    ).lastrowid
    object_id = conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'beta', 'Beta', 'place', 'now')"
    ).lastrowid
    conn.execute(
        "INSERT INTO kg_triples "
        "(user_id, subject_id, predicate, object_id, source_memory_id, "
        "source, confidence, created_at) "
        "VALUES (1, ?, 'lives_in', ?, ?, 'weekly_main', 1.0, 'now')",
        (subject, object_id, kept_id),
    )
    conn.commit()
    conn.close()

    vec_deletes = []
    original_vec_delete = db.vec_delete

    def track_vec_delete(item_ids, conn=None):
        vec_deletes.extend(item_ids)
        return original_vec_delete(item_ids, conn)

    monkeypatch.setattr(db, "vec_delete", track_vec_delete)
    assert update_memory_item(
        kept_id,
        1,
        content="Edited gamma memory",
        importance=2,
    )
    assert recall_memory(1, query="alpha") == []
    assert recall_memory(1, query="gamma")[0]["id"] == kept_id
    conn = _connect()
    assert conn.execute(
        "SELECT valid_to FROM kg_triples WHERE source_memory_id = ?",
        (kept_id,),
    ).fetchone()["valid_to"]
    assert conn.execute(
        "SELECT embedding FROM memory_items WHERE id = ?", (kept_id,),
    ).fetchone()["embedding"] is None
    conn.close()

    merge_memory_items(
        kept_id, [deleted_id], "Merged delta memory", new_importance=3,
    )
    assert recall_memory(1, query="gamma") == []
    assert recall_memory(1, query="beta") == []
    assert recall_memory(1, query="delta")[0]["id"] == kept_id

    trash = list_memory_trash(1)
    beta_trash = next(
        item for item in trash if "beta" in item["content"]
    )
    restored_id = restore_memory_from_trash(beta_trash["id"], 1)
    assert restored_id is not None
    assert any(
        item["id"] == restored_id
        for item in recall_memory(1, query="beta")
    )
    assert delete_memory_items([restored_id], deleted_by="test") == 1
    assert recall_memory(1, query="beta") == []
    assert {kept_id, deleted_id, restored_id} <= set(vec_deletes)


def _relationship_memory(content="Shiki lives with Mochi in Shanghai"):
    evidence_id = save_message(1, "user", content)
    item_id = save_memory_item(
        1,
        content,
        source="extracted",
        evidence_message_ids=[evidence_id],
    )
    conn = _connect()
    row = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    return item_id, {
        "item_id": item_id,
        "content": row["content"],
        "updated_at": row["updated_at"],
    }


def _relationship_upsert(source_memory):
    return {
        "op": "upsert",
        "subject": {"name": "Shiki", "type": "person"},
        "predicate": "lives_with",
        "object": {"name": "Mochi", "type": "pet"},
        "source_memory": source_memory,
    }


def test_weekly_relationship_upsert_archive_and_entity_recall():
    item_id, snapshot = _relationship_memory()
    conn = _connect()
    conn.execute(
        "INSERT INTO kg_entities "
        "(user_id, name, display_name, entity_type, created_at) "
        "VALUES (1, 'unused', 'Unused', 'place', 'now')"
    )
    conn.commit()
    conn.close()

    lives_in = {
        "op": "upsert",
        "subject": {"name": "Shiki", "type": "person"},
        "predicate": "lives_in",
        "object": {"name": "Shanghai", "type": "place"},
        "source_memory": snapshot,
    }
    created = curate_relationships(
        1, {item_id}, [_relationship_upsert(snapshot), lives_in],
    )

    assert len(created.upserted_ids) == 2
    assert set(find_matching_entities(
        1, "Shiki and Mochi are going home to Shanghai",
    )) == {"shiki", "mochi", "shanghai"}
    assert find_matching_entities(1, "The unused place") == []
    repeated = curate_relationships(
        1,
        {item_id},
        [_relationship_upsert(snapshot), lives_in],
    )
    assert repeated.upserted_ids == ()

    newer_id, newer_snapshot = _relationship_memory(
        "Shiki confirmed that Mochi still lives in the same household",
    )
    refreshed = curate_relationships(
        1, {newer_id}, [_relationship_upsert(newer_snapshot)],
    )
    assert len(refreshed.upserted_ids) == 1
    conn = _connect()
    assert conn.execute(
        "SELECT source_memory_id FROM kg_triples WHERE id = ?",
        (refreshed.upserted_ids[0],),
    ).fetchone()["source_memory_id"] == newer_id
    conn.close()
    relationships = list_active_relationships(1)

    archived = curate_relationships(
        1,
        {item_id},
        [
            {"op": "archive", "expected": relationship}
            for relationship in relationships
        ],
    )

    assert archived.archived_ids == tuple(
        relationship["triple_id"] for relationship in relationships
    )
    assert list_active_relationships(1) == []
    assert find_matching_entities(
        1, "Shiki, Mochi, and Shanghai",
    ) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", "concept"),
        ("predicate", "uses_tool"),
    ],
)
def test_weekly_relationship_rejects_invalid_scope_atomically(field, value):
    item_id, snapshot = _relationship_memory()
    invalid = _relationship_upsert(snapshot)
    if field == "type":
        invalid["object"]["type"] = value
    else:
        invalid["predicate"] = value

    with pytest.raises(RelationshipCurationError):
        curate_relationships(
            1,
            {item_id},
            [_relationship_upsert(snapshot), invalid],
        )

    conn = _connect()
    assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM kg_triples").fetchone()[0] == 0
    conn.close()


def test_weekly_relationship_requires_current_evidence_snapshot():
    item_id, snapshot = _relationship_memory()
    stale = dict(snapshot)
    stale["content"] = "stale"

    with pytest.raises(RelationshipCurationConflict):
        curate_relationships(1, {item_id}, [_relationship_upsert(stale)])

    unsupported_id = save_memory_item(
        1, "Shiki knows Mochi", source="admin",
    )
    conn = _connect()
    row = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (unsupported_id,),
    ).fetchone()
    conn.close()
    unsupported = {
        "item_id": unsupported_id,
        "content": row["content"],
        "updated_at": row["updated_at"],
    }
    with pytest.raises(RelationshipCurationError):
        curate_relationships(
            1, {unsupported_id}, [_relationship_upsert(unsupported)],
        )
