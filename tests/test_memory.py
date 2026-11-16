import json
from types import SimpleNamespace

from mochi import ai_client, config, db, memory_extraction as extraction, model_pool
from mochi.llm import LLMResponse
from tests.mock_llm import MockLLMProvider


def test_memory_retries_incomplete_input_and_keeps_corrections_searchable(monkeypatch):
    owner_id = 0  # The default WeChat owner is a valid memory scope.
    monkeypatch.setattr(extraction, "EXTRACTION_BATCH_SIZE", 2)
    monkeypatch.setattr(extraction, "OWNER_USER_ID", owner_id)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL", True)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_COOLDOWN", 0)
    monkeypatch.setattr(config, "MEMORY_AUTO_RECALL_MAX_TOKENS", 600)
    pool = SimpleNamespace(
        embed_batch=lambda texts: [None] * len(texts),
    )
    monkeypatch.setattr(extraction, "get_pool", lambda: pool)
    monkeypatch.setattr(model_pool, "get_pool", lambda: pool)
    evidence = db.save_message(owner_id, "user", "I like jasmine tea.", turn_id="one")
    db.save_message(owner_id, "assistant", "Noted.", turn_id="one")
    db.save_message(owner_id, "user", "Yes.", turn_id="two")
    boundary = db.save_message(owner_id, "assistant", "Understood.", turn_id="two")
    candidate = json.dumps([{
        "content": "Likes jasmine tea", "importance": 2, "evidence_message_ids": [evidence],
    }])
    client = MockLLMProvider([
        LLMResponse(content=candidate, finish_reason="length"),
        LLMResponse(content=candidate, finish_reason="stop"),
    ])
    monkeypatch.setattr(extraction, "get_client_for_tier", lambda _tier: client)
    assert extraction.drain_memory_extraction(owner_id) == 0
    assert db.get_memory_extraction_status(owner_id, 2)["last_processed_message_id"] == 0
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
    assert extraction.drain_memory_extraction(owner_id) == 1
    assert db.get_memory_extraction_status(owner_id, 2)["last_processed_message_id"] == boundary
    item = db.recall_memory(owner_id, query="jasmine", bump_access=False)[0]
    assert db.update_memory_item(item["id"], owner_id, content="Prefers coffee now", importance=2)
    assert db.recall_memory(owner_id, query="jasmine", bump_access=False) == []
    assert db.recall_memory(owner_id, query="coffee", bump_access=False)[0]["id"] == item["id"]
    recalled = ai_client._retrieve_memories_for_turn("coffee", owner_id)
    assert [memory["memory_id"] for memory in recalled] == [item["id"]]
    assert db.recall_memory(2, query="coffee", bump_access=False) == []
    assert ai_client._retrieve_memories_for_turn("coffee", 2) == []
    assert db.delete_memory_items([item["id"]], deleted_by="test") == 1
    assert db.recall_memory(owner_id, query="coffee", bump_access=False) == []
    trashed = db.list_memory_trash(owner_id)[0]
    restored = db.restore_memory_from_trash(trashed["id"], owner_id)
    assert db.recall_memory(owner_id, query="coffee", bump_access=False)[0]["id"] == restored
