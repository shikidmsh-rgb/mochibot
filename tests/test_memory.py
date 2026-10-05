import json
from types import SimpleNamespace

from mochi import db, memory_extraction as extraction
from mochi.llm import LLMResponse
from tests.mock_llm import MockLLMProvider


def test_memory_retries_incomplete_input_and_keeps_corrections_searchable(monkeypatch):
    monkeypatch.setattr(extraction, "EXTRACTION_BATCH_SIZE", 2)
    monkeypatch.setattr(extraction, "get_pool", lambda: SimpleNamespace(
        embed_batch=lambda texts: [None] * len(texts),
    ))
    evidence = db.save_message(1, "user", "I like jasmine tea.", turn_id="one")
    db.save_message(1, "assistant", "Noted.", turn_id="one")
    db.save_message(1, "user", "Yes.", turn_id="two")
    boundary = db.save_message(1, "assistant", "Understood.", turn_id="two")
    candidate = json.dumps([{
        "content": "Likes jasmine tea", "importance": 2, "evidence_message_ids": [evidence],
    }])
    client = MockLLMProvider([
        LLMResponse(content=candidate, finish_reason="length"),
        LLMResponse(content=candidate, finish_reason="stop"),
    ])
    monkeypatch.setattr(extraction, "get_client_for_tier", lambda _tier: client)
    assert extraction.drain_memory_extraction(1) == 0
    assert db.get_memory_extraction_status(1, 2)["last_processed_message_id"] == 0
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
    assert extraction.drain_memory_extraction(1) == 1
    assert db.get_memory_extraction_status(1, 2)["last_processed_message_id"] == boundary
    item = db.recall_memory(1, query="jasmine", bump_access=False)[0]
    assert db.update_memory_item(item["id"], 1, content="Prefers coffee now", importance=2)
    assert db.recall_memory(1, query="jasmine", bump_access=False) == []
    assert db.recall_memory(1, query="coffee", bump_access=False)[0]["id"] == item["id"]
    assert db.recall_memory(2, query="coffee", bump_access=False) == []
    assert db.delete_memory_items([item["id"]], deleted_by="test") == 1
    assert db.recall_memory(1, query="coffee", bump_access=False) == []
    trashed = db.list_memory_trash(1)[0]
    restored = db.restore_memory_from_trash(trashed["id"], 1)
    assert db.recall_memory(1, query="coffee", bump_access=False)[0]["id"] == restored
