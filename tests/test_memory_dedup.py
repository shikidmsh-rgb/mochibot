"""Automatic duplicate filtering must not erase changes in user memories."""

import json
import struct

import pytest

import mochi.memory_extraction as extraction
from mochi.admin.migration import _dedup_memory_items
from mochi.db import (
    _connect,
    get_memory_extraction_status,
    insert_memory_item,
    save_memory_item,
    save_message,
)
from mochi.llm import LLMResponse


CHANGED_CONTENT = [
    pytest.param(
        "\u559c\u6b22\u5468\u672b\u53bb\u722c\u5c71",
        "\u4e0d\u559c\u6b22\u5468\u672b\u53bb\u722c\u5c71",
        id="negation",
    ),
    pytest.param(
        "Plans to move to Seattle",
        "Plans to move to Seattle next year",
        id="added-detail",
    ),
    pytest.param(
        "Plans to move to Seattle in 2026",
        "Plans to move to Seattle in 2027",
        id="high-similarity-correction",
    ),
    pytest.param(
        "Aims to run 1.5 km daily",
        "Aims to run 15 km daily",
        id="decimal-point",
    ),
    pytest.param(
        "Recorded temperature is -5 C",
        "Recorded temperature is 5 C",
        id="negative-sign",
    ),
    pytest.param(
        "Uses label ABC for work",
        "Uses label abc for work",
        id="case-sensitive-label",
    ),
]


def _memory_rows():
    conn = _connect()
    try:
        return [
            dict(row)
            for row in conn.execute("SELECT * FROM memory_items ORDER BY id")
        ]
    finally:
        conn.close()


def _extract_pair(monkeypatch, old, new, location):
    evidence = []
    for number, content in enumerate((old, new)):
        turn_id = f"dedup-{number}"
        evidence.append(save_message(1, "user", content, turn_id=turn_id))
        boundary = save_message(1, "assistant", "Noted.", turn_id=turn_id)

    core = f"# Preferences\n\n- {old}" if location == "core" else ""
    monkeypatch.setattr(extraction, "read_core", lambda: core)
    monkeypatch.setattr(extraction, "EXTRACTION_BATCH_SIZE", 2)

    class Pool:
        def embed_batch(self, texts):
            return [None] * len(texts)

    monkeypatch.setattr(extraction, "get_pool", Pool)
    if location == "stored":
        insert_memory_item(
            1, old, 2, source="admin", evidence_message_ids=[evidence[0]],
        )
    before = _memory_rows()
    candidates = [
        {"content": content, "importance": 2, "evidence_message_ids": [message_id]}
        for content, message_id in zip((old, new), evidence)
    ]
    if location != "batch":
        candidates = candidates[1:]

    class Client:
        def chat(self, **kwargs):
            return LLMResponse(
                content=json.dumps(candidates), model="lite-test",
            )

    monkeypatch.setattr(extraction, "get_client_for_tier", lambda _tier: Client())
    inserted = extraction.drain_memory_extraction(1)
    status = get_memory_extraction_status(1, 2)
    assert status["pending_turns"] == 0
    assert status["last_processed_message_id"] == boundary
    return inserted, before, _memory_rows(), evidence


@pytest.mark.parametrize("old,new", CHANGED_CONTENT)
@pytest.mark.parametrize("location", ["batch", "core", "stored"])
def test_extraction_preserves_changed_content(monkeypatch, old, new, location):
    inserted, before, rows, evidence = _extract_pair(
        monkeypatch, old, new, location,
    )

    assert inserted == (2 if location == "batch" else 1)
    assert [row["content"] for row in rows] == (
        [new] if location == "core" else [old, new]
    )
    assert json.loads(rows[-1]["evidence_message_ids"]) == [evidence[1]]
    if location == "stored":
        assert rows[0] == before[0]
    elif location == "batch":
        assert json.loads(rows[0]["evidence_message_ids"]) == [evidence[0]]


@pytest.mark.parametrize("new", ["Likes jasmine tea", "  Likes  jasmine\ttea  "])
@pytest.mark.parametrize("location", ["batch", "core", "stored"])
def test_extraction_still_filters_equal_content(monkeypatch, new, location):
    inserted, before, rows, _ = _extract_pair(
        monkeypatch, "Likes jasmine tea", new, location,
    )

    assert inserted == (1 if location == "batch" else 0)
    assert [row["content"] for row in rows] == (
        [] if location == "core" else ["Likes jasmine tea"]
    )
    if location == "stored":
        assert rows == before


def test_core_comparison_does_not_strip_a_negative_sign_as_a_list_marker():
    candidate = {
        "content": "5 C",
        "importance": 1,
        "evidence_message_ids": [1],
    }

    assert extraction._filter_core_duplicates([candidate], "-5 C") == [candidate]
    assert extraction._filter_core_duplicates([candidate], "- 5 C") == []


@pytest.mark.parametrize("old,new", CHANGED_CONTENT)
@pytest.mark.parametrize("with_embedding", [False, True])
def test_save_keeps_changed_content_and_its_evidence(old, new, with_embedding):
    embedding = (
        struct.pack("1536f", 1.0, *([0.0] * 1535))
        if with_embedding else None
    )
    old_evidence = save_message(1, "user", old)
    new_evidence = save_message(1, "user", new)
    first_id = save_memory_item(
        1, old, embedding=embedding, evidence_message_ids=[old_evidence],
    )
    before = _memory_rows()[0]
    second_id = save_memory_item(
        1, new, embedding=embedding, evidence_message_ids=[new_evidence],
    )

    assert first_id != second_id
    rows = _memory_rows()
    assert rows[0] == before
    assert rows[1]["content"] == new
    assert json.loads(rows[1]["evidence_message_ids"]) == [new_evidence]


@pytest.mark.parametrize("new", ["Likes jasmine tea", "  Likes  jasmine\ttea  "])
def test_save_equal_content_merges_evidence_without_rewriting_memory(new):
    first_evidence = save_message(1, "user", "Likes jasmine tea")
    second_evidence = save_message(1, "user", new)
    first_id = save_memory_item(
        1, "Likes jasmine tea", evidence_message_ids=[first_evidence],
    )
    before = _memory_rows()[0]

    assert save_memory_item(
        1, new, evidence_message_ids=[second_evidence],
    ) == first_id
    rows = _memory_rows()
    assert len(rows) == 1
    expected = {
        **before,
        "evidence_message_ids": json.dumps(
            [first_evidence, second_evidence], separators=(",", ":"),
        ),
    }
    assert rows[0] == expected


@pytest.mark.parametrize("old,new", [
    *CHANGED_CONTENT,
    pytest.param(
        "[2026-01-01] Started learning Japanese",
        "[2026-02-01] Started learning Japanese",
        id="different-event-dates",
    ),
])
def test_import_keeps_changed_memories(old, new):
    items = [
        {"content": old, "importance": 2},
        {"content": new, "importance": 2},
    ]

    assert _dedup_memory_items(items) == items


def test_import_only_collapses_whitespace_equal_content():
    items = [
        {"content": "Likes jasmine tea", "importance": 2},
        {"content": " \tLikes  jasmine\ttea ", "importance": 2},
        {"content": "Likes jasminetea", "importance": 2},
    ]

    assert _dedup_memory_items(items) == [items[0], items[2]]
