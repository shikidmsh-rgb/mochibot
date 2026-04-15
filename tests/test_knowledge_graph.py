"""Tests for mochi/knowledge_graph.py — KG entity and triple CRUD."""

import pytest
from datetime import datetime, timezone, timedelta

from mochi.knowledge_graph import (
    _normalize_name,
    get_or_create_entity,
    get_entity_by_name,
    list_entities,
    add_triple,
    invalidate_triple,
    query_entity,
    find_matching_entities,
    entity_context_for_prompt,
    get_kg_stats,
    cleanup_expired_triples,
    SINGLE_VALUED_PREDICATES,
)

UID = 1


# ── _normalize_name ──────────────────────────────────────────────────

class TestNormalizeName:
    def test_basic_lowercase(self):
        assert _normalize_name("Alice") == "alice"

    def test_strip_emoji(self):
        assert _normalize_name("小白🐱") == "小白"

    def test_unicode_normalization(self):
        # NFKC: ＡＢＣ → ABC
        assert _normalize_name("ＡＢＣ") == "abc"

    def test_collapse_whitespace(self):
        assert _normalize_name("  hello   world  ") == "hello world"

    def test_strip_parentheses(self):
        assert _normalize_name("Alice (Bob)") == "alice bob"
        assert _normalize_name("猫（橘）") == "猫橘"

    def test_empty_after_normalize(self):
        # Pure emoji → empty
        assert _normalize_name("🐱🐶") == ""

    def test_chinese_name(self):
        assert _normalize_name("小白") == "小白"


# ── get_or_create_entity ─────────────────────────────────────────────

class TestGetOrCreateEntity:
    def test_create_new(self):
        eid = get_or_create_entity(UID, "小白", entity_type="pet")
        assert isinstance(eid, int) and eid > 0

    def test_idempotent(self):
        eid1 = get_or_create_entity(UID, "小白", entity_type="pet")
        eid2 = get_or_create_entity(UID, "小白", entity_type="pet")
        assert eid1 == eid2

    def test_case_insensitive(self):
        eid1 = get_or_create_entity(UID, "Alice", entity_type="person")
        eid2 = get_or_create_entity(UID, "alice", entity_type="person")
        assert eid1 == eid2

    def test_emoji_stripped_idempotent(self):
        eid1 = get_or_create_entity(UID, "小白🐱", entity_type="pet")
        eid2 = get_or_create_entity(UID, "小白", entity_type="pet")
        assert eid1 == eid2

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            get_or_create_entity(UID, "🐱")  # normalizes to empty

    def test_update_display_name_if_richer(self):
        get_or_create_entity(UID, "cat", entity_type="pet", display_name="Cat")
        get_or_create_entity(UID, "cat", entity_type="pet", display_name="Cat the Great")
        entity = get_entity_by_name(UID, "cat")
        assert entity["display_name"] == "Cat the Great"

    def test_user_isolation(self):
        eid1 = get_or_create_entity(1, "shared", entity_type="concept")
        eid2 = get_or_create_entity(2, "shared", entity_type="concept")
        assert eid1 != eid2


# ── get_entity_by_name ───────────────────────────────────────────────

class TestGetEntityByName:
    def test_found(self):
        get_or_create_entity(UID, "小白", entity_type="pet")
        entity = get_entity_by_name(UID, "小白")
        assert entity is not None
        assert entity["name"] == "小白"
        assert entity["entity_type"] == "pet"

    def test_not_found(self):
        assert get_entity_by_name(UID, "nonexistent") is None

    def test_empty_name(self):
        assert get_entity_by_name(UID, "🐱") is None


# ── add_triple ───────────────────────────────────────────────────────

class TestAddTriple:
    def test_basic(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        tid = add_triple(UID, s, "has_condition", o)
        assert isinstance(tid, int) and tid > 0

    def test_idempotent(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        tid1 = add_triple(UID, s, "has_condition", o)
        tid2 = add_triple(UID, s, "has_condition", o)
        assert tid1 == tid2

    def test_single_valued_auto_invalidation(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o1 = get_or_create_entity(UID, "5kg", entity_type="concept")
        o2 = get_or_create_entity(UID, "6kg", entity_type="concept")

        tid1 = add_triple(UID, s, "weighs", o1)
        tid2 = add_triple(UID, s, "weighs", o2)
        assert tid1 != tid2

        # Query: only the new triple should be active
        result = query_entity(UID, "小白")
        active_weighs = [
            t for t in result["as_subject"] if t["predicate"] == "weighs"
        ]
        assert len(active_weighs) == 1
        assert active_weighs[0]["object_name"] == "6kg"

    def test_multi_valued_no_invalidation(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o1 = get_or_create_entity(UID, "感冒", entity_type="concept")
        o2 = get_or_create_entity(UID, "过敏", entity_type="concept")

        add_triple(UID, s, "has_condition", o1)
        add_triple(UID, s, "has_condition", o2)

        # Both should be active (has_condition is multi-valued)
        result = query_entity(UID, "小白")
        active_conds = [
            t for t in result["as_subject"] if t["predicate"] == "has_condition"
        ]
        assert len(active_conds) == 2


# ── query_entity ─────────────────────────────────────────────────────

class TestQueryEntity:
    def test_with_relationships(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        add_triple(UID, s, "has_condition", o)

        result = query_entity(UID, "小白")
        assert result is not None
        assert result["entity"]["name"] == "小白"
        assert len(result["as_subject"]) == 1
        assert result["as_subject"][0]["predicate"] == "has_condition"

    def test_not_found(self):
        assert query_entity(UID, "nonexistent") is None

    def test_as_object(self):
        s = get_or_create_entity(UID, "owner", entity_type="person")
        o = get_or_create_entity(UID, "小白", entity_type="pet")
        add_triple(UID, s, "owns", o)

        result = query_entity(UID, "小白")
        assert len(result["as_object"]) == 1
        assert result["as_object"][0]["predicate"] == "owns"
        assert result["as_object"][0]["subject_name"] == "owner"


# ── find_matching_entities ───────────────────────────────────────────

class TestFindMatchingEntities:
    def test_basic_match(self):
        get_or_create_entity(UID, "小白", entity_type="pet")
        matched = find_matching_entities(UID, "今天小白怎么样")
        assert "小白" in matched

    def test_no_match(self):
        get_or_create_entity(UID, "小白", entity_type="pet")
        matched = find_matching_entities(UID, "今天天气真好")
        assert matched == []

    def test_type_filter(self):
        get_or_create_entity(UID, "东京", entity_type="place")
        # Default matchable_types is ("person", "pet"), so place won't match
        matched = find_matching_entities(UID, "东京好玩吗")
        assert matched == []

    def test_min_length_filter(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "KG_ENTITY_MATCH_MIN_LENGTH", 3)
        get_or_create_entity(UID, "AB", entity_type="pet")
        matched = find_matching_entities(UID, "AB is here")
        assert matched == []


# ── entity_context_for_prompt ────────────────────────────────────────

class TestEntityContextForPrompt:
    def test_basic_format(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet", display_name="小白")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        add_triple(UID, s, "has_condition", o)

        text = entity_context_for_prompt(UID, "小白")
        assert "小白" in text
        assert "has_condition" in text
        assert "感冒" in text

    def test_empty_for_unknown(self):
        assert entity_context_for_prompt(UID, "nonexistent") == ""

    def test_empty_for_no_triples(self):
        get_or_create_entity(UID, "lonely", entity_type="pet")
        assert entity_context_for_prompt(UID, "lonely") == ""


# ── get_kg_stats ─────────────────────────────────────────────────────

class TestGetKgStats:
    def test_empty(self):
        stats = get_kg_stats(UID)
        assert stats == {"entities": 0, "active_triples": 0, "total_triples": 0}

    def test_with_data(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        add_triple(UID, s, "has_condition", o)

        stats = get_kg_stats(UID)
        assert stats["entities"] == 2
        assert stats["active_triples"] == 1
        assert stats["total_triples"] == 1


# ── cleanup_expired_triples ──────────────────────────────────────────

class TestCleanupExpiredTriples:
    def test_cleanup_old(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o1 = get_or_create_entity(UID, "5kg", entity_type="concept")
        o2 = get_or_create_entity(UID, "6kg", entity_type="concept")

        # Add first triple, then override with second (auto-invalidates first)
        add_triple(UID, s, "weighs", o1)
        add_triple(UID, s, "weighs", o2)

        # The old triple has valid_to set to "now", which is not 90 days old yet
        purged = cleanup_expired_triples(days=90)
        assert purged == 0

        # Force-expire it by setting valid_to to 100 days ago
        from mochi.db import _connect
        old_date = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        conn = _connect()
        conn.execute(
            "UPDATE kg_triples SET valid_to = ? WHERE valid_to IS NOT NULL",
            (old_date,),
        )
        conn.commit()
        conn.close()

        purged = cleanup_expired_triples(days=90)
        assert purged == 1

    def test_no_delete_active(self):
        s = get_or_create_entity(UID, "cat", entity_type="pet")
        o = get_or_create_entity(UID, "happy", entity_type="concept")
        add_triple(UID, s, "has_status", o)

        purged = cleanup_expired_triples(days=0)
        assert purged == 0  # active triple (valid_to IS NULL) should NOT be deleted


# ── extract_kg (integration with mock LLM) ──────────────────────────

class TestExtractKg:
    def test_extract_with_mock_llm(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "KG_ENABLED", True)

        # Seed a conversation (long enough to pass 50-char threshold)
        from mochi.db import save_message
        save_message(UID, "user", "我家猫小白最近去看了医生，医生说她得了感冒，需要吃药控制")
        save_message(UID, "assistant", "小白快点好起来！感冒是可以通过药物控制的")

        # Mock LLM response
        class MockResponse:
            content = '{"entities":[{"name":"小白","type":"pet"}],"triples":[{"subject":"小白","predicate":"has_condition","object":"感冒"}]}'
            prompt_tokens = 100
            completion_tokens = 50
            total_tokens = 150
            model = "test-model"

        class MockClient:
            def chat(self, **kwargs):
                return MockResponse()

        monkeypatch.setattr(
            "mochi.memory_engine.get_client_for_tier",
            lambda tier: MockClient(),
        )

        from mochi.memory_engine import extract_kg
        result = extract_kg(UID)

        assert result["entities"] == 1
        assert result["triples"] == 1

        # Verify entity exists in DB
        entity = get_entity_by_name(UID, "小白")
        assert entity is not None
        assert entity["entity_type"] == "pet"

        # Verify triple exists
        q = query_entity(UID, "小白")
        assert len(q["as_subject"]) == 1
        assert q["as_subject"][0]["predicate"] == "has_condition"

    def test_skip_when_disabled(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "KG_ENABLED", False)

        from mochi.memory_engine import extract_kg
        result = extract_kg(UID)
        assert result == {}

    def test_skip_short_conversations(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "KG_ENABLED", True)

        from mochi.db import save_message
        save_message(UID, "user", "hi")

        from mochi.memory_engine import extract_kg
        result = extract_kg(UID)
        assert result.get("entities", 0) == 0


# ── list_entities ────────────────────────────────────────────────────

class TestListEntities:
    def test_empty(self):
        assert list_entities(UID) == []

    def test_filter_by_type(self):
        get_or_create_entity(UID, "小白", entity_type="pet")
        get_or_create_entity(UID, "东京", entity_type="place")

        pets = list_entities(UID, entity_type="pet")
        assert len(pets) == 1
        assert pets[0]["entity_type"] == "pet"

        all_ents = list_entities(UID)
        assert len(all_ents) == 2


# ── invalidate_triple ────────────────────────────────────────────────

class TestInvalidateTriple:
    def test_invalidate(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        tid = add_triple(UID, s, "has_condition", o)

        assert invalidate_triple(tid) is True
        # After invalidation, query should show no active triples
        result = query_entity(UID, "小白")
        assert result["as_subject"] == []

    def test_invalidate_already_invalid(self):
        s = get_or_create_entity(UID, "小白", entity_type="pet")
        o = get_or_create_entity(UID, "感冒", entity_type="concept")
        tid = add_triple(UID, s, "has_condition", o)

        invalidate_triple(tid)
        assert invalidate_triple(tid) is False  # already invalidated
