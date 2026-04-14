"""Tests for mochi/memory_engine.py — JSON parsing, extract/dedup/outdated/salience/audit."""

import pytest
from unittest.mock import patch, MagicMock


# ── JSON Parsing ──

class TestParseGptJson:

    def _parse(self, raw):
        from mochi.memory_engine import _parse_gpt_json
        return _parse_gpt_json(raw)

    def test_valid_object(self):
        assert self._parse('{"key": "value"}') == {"key": "value"}

    def test_valid_array(self):
        assert self._parse('[1, 2, 3]') == [1, 2, 3]

    def test_markdown_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert self._parse(raw) == {"a": 1}

    def test_markdown_fence_no_lang(self):
        raw = '```\n{"a": 1}\n```'
        assert self._parse(raw) == {"a": 1}

    def test_trailing_comma_object(self):
        assert self._parse('{"a": 1,}') == {"a": 1}

    def test_trailing_comma_array(self):
        assert self._parse('[1, 2,]') == [1, 2]

    def test_json_in_surrounding_text(self):
        raw = 'Here is the result: {"key": "val"} done!'
        assert self._parse(raw) == {"key": "val"}

    def test_array_in_surrounding_text(self):
        """_parse_gpt_json extracts {} before [] — so inner dict is matched."""
        raw = 'Extracted: [{"content": "likes tea"}] end'
        result = self._parse(raw)
        # The parser tries {} pattern first, so it extracts the inner dict
        assert isinstance(result, dict)
        assert result["content"] == "likes tea"

    def test_completely_invalid(self):
        assert self._parse("not json at all") == {}

    def test_empty_string(self):
        assert self._parse("") == {}

    def test_whitespace_only(self):
        assert self._parse("   \n  ") == {}

    def test_unicode_content(self):
        raw = '{"content": "喜欢喝茶"}'
        assert self._parse(raw)["content"] == "喜欢喝茶"

    def test_nested_json(self):
        raw = '{"outer": {"inner": [1, 2]}}'
        result = self._parse(raw)
        assert result["outer"]["inner"] == [1, 2]


# ── Helper: mock LLM response ──

def _mock_llm_response(content="", prompt_tokens=10, completion_tokens=5):
    resp = MagicMock()
    resp.content = content
    resp.prompt_tokens = prompt_tokens
    resp.completion_tokens = completion_tokens
    resp.total_tokens = prompt_tokens + completion_tokens
    resp.model = "test-model"
    return resp


def _mock_client(content=""):
    client = MagicMock()
    client.chat.return_value = _mock_llm_response(content)
    return client


# ── Extract Memories ──

class TestExtractMemories:

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_no_unprocessed(self, mock_get_conv, mock_prompt, mock_client,
                            mock_save, mock_mark, mock_log):
        mock_get_conv.return_value = []
        from mochi.memory_engine import extract_memories
        assert extract_memories(1) == 0
        mock_client.assert_not_called()

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_prompt_missing(self, mock_get_conv, mock_prompt, mock_client,
                            mock_save, mock_mark, mock_log):
        mock_get_conv.return_value = [
            {"id": 1, "created_at": "2025-01-01", "role": "user", "content": "hi"}
        ]
        mock_prompt.return_value = None
        from mochi.memory_engine import extract_memories
        assert extract_memories(1) == 0

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_successful_extraction(self, mock_get_conv, mock_prompt, mock_client_fn,
                                    mock_save, mock_mark, mock_log):
        mock_get_conv.return_value = [
            {"id": 5, "created_at": "2025-01-01", "role": "user", "content": "I love tea"}
        ]
        mock_prompt.return_value = "Extract memories"
        client = _mock_client('[{"content": "likes tea", "category": "preference", "importance": 2}]')
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        assert extract_memories(1) == 1
        mock_save.assert_called_once()
        mock_mark.assert_called_once_with(1, 5)

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_dict_with_memories_key(self, mock_get_conv, mock_prompt, mock_client_fn,
                                     mock_save, mock_mark, mock_log):
        mock_get_conv.return_value = [
            {"id": 1, "created_at": "2025-01-01", "role": "user", "content": "hi"}
        ]
        mock_prompt.return_value = "Extract"
        client = _mock_client('{"memories": [{"content": "fact", "category": "其他"}]}')
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        assert extract_memories(1) == 1

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_invalid_json_returns_zero(self, mock_get_conv, mock_prompt, mock_client_fn,
                                       mock_save, mock_mark, mock_log):
        mock_get_conv.return_value = [
            {"id": 1, "created_at": "2025-01-01", "role": "user", "content": "hi"}
        ]
        mock_prompt.return_value = "Extract"
        client = _mock_client("Sorry, I can't extract anything.")
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        assert extract_memories(1) == 0


# ── Deduplicate Memories ──

class TestDeduplicateMemories:

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.merge_memory_items")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_too_few_skips(self, mock_items, mock_client, mock_merge, mock_log):
        mock_items.return_value = [
            {"id": i, "category": "general", "content": f"item{i}", "importance": 1}
            for i in range(3)
        ]
        from mochi.memory_engine import deduplicate_memories
        assert deduplicate_memories(1) == 0
        mock_client.assert_not_called()

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.merge_memory_items")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_no_merge_needed(self, mock_items, mock_client_fn, mock_merge, mock_log):
        mock_items.return_value = [
            {"id": i, "category": "general", "content": f"unique item {i}", "importance": 1}
            for i in range(6)
        ]
        client = _mock_client('{"operations": []}')
        mock_client_fn.return_value = client

        from mochi.memory_engine import deduplicate_memories
        assert deduplicate_memories(1) == 0
        mock_merge.assert_not_called()

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.merge_memory_items")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_successful_merge(self, mock_items, mock_client_fn, mock_merge, mock_log):
        mock_items.return_value = [
            {"id": i, "category": "preference", "content": f"likes tea {i}", "importance": 1}
            for i in range(6)
        ]
        client = _mock_client(
            '{"operations": [{"keep": 0, "delete": [1], "merged_content": "likes tea", "importance": 2}]}'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import deduplicate_memories
        assert deduplicate_memories(1) == 1
        mock_merge.assert_called_once_with(0, [1], "likes tea", new_importance=2)


# ── Remove Outdated ──

class TestRemoveOutdatedMemories:

    @patch("mochi.memory_engine.get_all_memory_items")
    def test_no_items(self, mock_items):
        mock_items.return_value = []
        from mochi.memory_engine import remove_outdated_memories
        result = remove_outdated_memories(1)
        assert result == {"deleted": 0, "errors": 0}

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.delete_memory_items")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_successful_deletion(self, mock_items, mock_client_fn, mock_delete, mock_log):
        mock_items.return_value = [
            {"id": 1, "category": "event", "content": "meeting next week",
             "importance": 1, "created_at": "2025-01-01 00:00", "updated_at": "2025-01-01 00:00"}
        ]
        client = _mock_client(
            '{"operations": [{"item_id": 1, "action": "delete", "reason": "deadline passed"}]}'
        )
        mock_client_fn.return_value = client
        mock_delete.return_value = 1

        from mochi.memory_engine import remove_outdated_memories
        result = remove_outdated_memories(1)
        assert result["deleted"] == 1
        mock_delete.assert_called_once_with([1], deleted_by="maintenance")

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_llm_failure_error_count(self, mock_items, mock_client_fn, mock_log):
        mock_items.return_value = [
            {"id": i, "category": "general", "content": f"item {i}",
             "importance": 1, "created_at": "2025-01-01 00:00", "updated_at": "2025-01-01 00:00"}
            for i in range(3)
        ]
        client = MagicMock()
        client.chat.side_effect = Exception("API error")
        mock_client_fn.return_value = client

        from mochi.memory_engine import remove_outdated_memories
        result = remove_outdated_memories(1)
        assert result["errors"] >= 1


# ── Salience Rebalancing ──

class TestRebalanceSalience:

    @patch("mochi.memory_engine.get_all_memory_items")
    def test_no_candidates(self, mock_items):
        mock_items.return_value = []
        from mochi.memory_engine import rebalance_salience
        result = rebalance_salience(1)
        assert result == {"promoted": 0, "demoted": 0}

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.update_memory_importance")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_promote_selection(self, mock_items, mock_client_fn, mock_update, mock_log,
                                monkeypatch):
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "MEMORY_DEMOTE_MIN_ACCESS", 3)
        mock_items.return_value = [
            {"id": 1, "importance": 1, "access_count": 5, "category": "preference",
             "content": "likes tea", "created_at": "2025-01-01", "updated_at": "2025-01-01",
             "last_accessed": "2025-01-01"}
        ]
        client = _mock_client(
            '{"operations": [{"item_id": 1, "action": "promote", "new_importance": 2, "reason": "frequently accessed"}]}'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import rebalance_salience
        result = rebalance_salience(1)
        assert result["promoted"] == 1
        mock_update.assert_called_once_with(1, 2)

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.update_memory_importance")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_safety_blocks_importance_3(self, mock_items, mock_client_fn, mock_update, mock_log,
                                         monkeypatch):
        """Never allow setting importance to 3 via salience."""
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "MEMORY_DEMOTE_MIN_ACCESS", 3)
        mock_items.return_value = [
            {"id": 1, "importance": 1, "access_count": 10, "category": "preference",
             "content": "test", "created_at": "2025-01-01", "updated_at": "2025-01-01",
             "last_accessed": "2025-01-01"}
        ]
        client = _mock_client(
            '{"operations": [{"item_id": 1, "action": "promote", "new_importance": 3, "reason": "critical"}]}'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import rebalance_salience
        result = rebalance_salience(1)
        assert result["promoted"] == 0
        mock_update.assert_not_called()

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.update_memory_importance")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_safety_only_1_or_2(self, mock_items, mock_client_fn, mock_update, mock_log,
                                 monkeypatch):
        """Only importance values 1 and 2 are allowed."""
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "MEMORY_DEMOTE_MIN_ACCESS", 3)
        mock_items.return_value = [
            {"id": 1, "importance": 1, "access_count": 10, "category": "preference",
             "content": "test", "created_at": "2025-01-01", "updated_at": "2025-01-01",
             "last_accessed": "2025-01-01"}
        ]
        client = _mock_client(
            '{"operations": [{"item_id": 1, "action": "promote", "new_importance": 5, "reason": "high"}]}'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import rebalance_salience
        result = rebalance_salience(1)
        assert result["promoted"] == 0

    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_all_memory_items")
    def test_llm_failure_returns_zeros(self, mock_items, mock_client_fn, monkeypatch):
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "MEMORY_DEMOTE_MIN_ACCESS", 3)
        mock_items.return_value = [
            {"id": 1, "importance": 1, "access_count": 5, "category": "preference",
             "content": "test", "created_at": "2025-01-01", "updated_at": "2025-01-01",
             "last_accessed": "2025-01-01"}
        ]
        client = MagicMock()
        client.chat.side_effect = Exception("API error")
        mock_client_fn.return_value = client

        from mochi.memory_engine import rebalance_salience
        result = rebalance_salience(1)
        assert result == {"promoted": 0, "demoted": 0}


# ── Core Memory Audit ──

class TestAuditCoreMemoryTokens:

    @patch("mochi.memory_engine.get_core_memory")
    def test_empty_core(self, mock_core):
        mock_core.return_value = ""
        from mochi.memory_engine import audit_core_memory_tokens
        result = audit_core_memory_tokens(1)
        assert result["status"] == "empty"
        assert result["tokens"] == 0
        assert result["over_budget"] is False

    @patch("mochi.memory_engine.get_core_memory")
    def test_under_budget(self, mock_core, monkeypatch):
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "CORE_MEMORY_MAX_TOKENS", 10000)
        mock_core.return_value = "short content"
        from mochi.memory_engine import audit_core_memory_tokens
        result = audit_core_memory_tokens(1)
        assert result["status"] == "ok"
        assert result["over_budget"] is False

    @patch("mochi.memory_engine.get_core_memory")
    def test_over_budget(self, mock_core, monkeypatch):
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "CORE_MEMORY_MAX_TOKENS", 5)
        mock_core.return_value = "This is a very long core memory that exceeds the budget."
        from mochi.memory_engine import audit_core_memory_tokens
        result = audit_core_memory_tokens(1)
        assert result["status"] == "over_budget"
        assert result["over_budget"] is True


# ── Smart Maintenance ──

class TestSmartMaintenance:

    @patch("mochi.memory_engine.cleanup_old_trash", return_value=2)
    @patch("mochi.memory_engine.audit_core_memory_tokens",
           return_value={"status": "ok", "tokens": 100, "over_budget": False})
    @patch("mochi.memory_engine.rebalance_salience",
           return_value={"promoted": 1, "demoted": 0})
    @patch("mochi.memory_engine.remove_outdated_memories",
           return_value={"deleted": 3, "errors": 0})
    @patch("mochi.memory_engine.deduplicate_memories", return_value=1)
    @patch("mochi.memory_engine.extract_memories", return_value=5)
    def test_all_steps_called(self, mock_extract, mock_dedup, mock_outdated,
                               mock_salience, mock_audit, mock_trash):
        from mochi.memory_engine import smart_maintenance
        result = smart_maintenance(1)
        assert result["extracted"] == 5
        assert result["deduplicated"] == 1
        assert result["outdated"]["deleted"] == 3
        assert result["salience"]["promoted"] == 1
        assert result["core_audit"]["status"] == "ok"
        assert result["trash_purged"] == 2

    @patch("mochi.memory_engine.cleanup_old_trash", return_value=0)
    @patch("mochi.memory_engine.audit_core_memory_tokens",
           return_value={"status": "ok", "tokens": 0, "over_budget": False})
    @patch("mochi.memory_engine.rebalance_salience",
           return_value={"promoted": 0, "demoted": 0})
    @patch("mochi.memory_engine.remove_outdated_memories",
           return_value={"deleted": 0, "errors": 0})
    @patch("mochi.memory_engine.deduplicate_memories")
    @patch("mochi.memory_engine.extract_memories", return_value=0)
    def test_step_failure_continues(self, mock_extract, mock_dedup, mock_outdated,
                                     mock_salience, mock_audit, mock_trash):
        """One step failing should not prevent others from running."""
        mock_dedup.side_effect = Exception("DB error")
        from mochi.memory_engine import smart_maintenance
        result = smart_maintenance(1)
        # Other steps should still have run
        assert result["extracted"] == 0
        assert result["outdated"]["deleted"] == 0


# ── Relational → Core Memory Auto-Append ──

class TestAppendRelationalToCore:

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_append_to_empty_core(self, mock_get_core, mock_update):
        """Relational items should be appended when core memory is empty."""
        mock_get_core.return_value = ""
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["吵架后喜欢冷静一天再沟通"])
        mock_update.assert_called_once_with(1, "- 吵架后喜欢冷静一天再沟通")

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_append_to_existing_core(self, mock_get_core, mock_update):
        """Items should be appended after existing core content."""
        mock_get_core.return_value = "- 用户名叫小明"
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["说AI凶的时候反而觉得真实"])
        args = mock_update.call_args[0]
        assert args[1] == "- 用户名叫小明\n- 说AI凶的时候反而觉得真实"

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_dedup_exact_match(self, mock_get_core, mock_update):
        """Items already in core memory should be skipped."""
        mock_get_core.return_value = "- 吵架后喜欢冷静一天再沟通"
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["吵架后喜欢冷静一天再沟通"])
        mock_update.assert_not_called()

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_dedup_case_insensitive(self, mock_get_core, mock_update):
        """Dedup should be case-insensitive."""
        mock_get_core.return_value = "- Inside Joke About Coffee"
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["inside joke about coffee"])
        mock_update.assert_not_called()

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_dedup_strips_prefix(self, mock_get_core, mock_update):
        """Dedup should strip '- ' prefix before comparing."""
        mock_get_core.return_value = "- 心情不好别讲大道理"
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["心情不好别讲大道理"])
        mock_update.assert_not_called()

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_max_per_cycle_cap(self, mock_get_core, mock_update):
        """At most _MAX_RELATIONAL_PER_CYCLE items should be appended."""
        mock_get_core.return_value = ""
        from mochi.memory_engine import _append_relational_to_core
        items = [f"relational item {i}" for i in range(10)]
        _append_relational_to_core(1, items)
        written = mock_update.call_args[0][1]
        assert written.count("- ") == 3  # _MAX_RELATIONAL_PER_CYCLE = 3

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_token_budget_exceeded_skips(self, mock_get_core, mock_update, monkeypatch):
        """Should skip append when core memory exceeds token budget."""
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "_RELATIONAL_TOKEN_BUDGET", 5)
        # A long string that certainly exceeds 5 tokens
        mock_get_core.return_value = "This is a core memory that is definitely longer than five tokens in length."
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["新的关系记忆"])
        mock_update.assert_not_called()

    @patch("mochi.memory_engine.update_core_memory")
    @patch("mochi.memory_engine.get_core_memory")
    def test_mixed_new_and_existing(self, mock_get_core, mock_update):
        """Only genuinely new items should be appended, existing ones skipped."""
        mock_get_core.return_value = "- 已有的关系记忆"
        from mochi.memory_engine import _append_relational_to_core
        _append_relational_to_core(1, ["已有的关系记忆", "全新的关系记忆"])
        args = mock_update.call_args[0]
        assert "全新的关系记忆" in args[1]
        assert args[1].count("已有的关系记忆") == 1  # only the original


class TestExtractMemoriesRelational:
    """Test that extract_memories routes 关系 items to core_memory."""

    @patch("mochi.memory_engine._append_relational_to_core")
    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_relational_triggers_core_append(self, mock_get_conv, mock_prompt,
                                              mock_client_fn, mock_save, mock_mark,
                                              mock_log, mock_append):
        """When LLM returns a 关系 item, _append_relational_to_core should be called."""
        mock_get_conv.return_value = [
            {"id": 10, "created_at": "2025-01-01", "role": "user",
             "content": "你凶我的时候我反而觉得安心"}
        ]
        mock_prompt.return_value = "Extract"
        client = _mock_client(
            '[{"content": "用户觉得AI凶的时候反而安心", "category": "关系", "importance": 2}]'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        count = extract_memories(1)
        assert count == 1
        mock_save.assert_called_once()
        mock_append.assert_called_once_with(1, ["用户觉得AI凶的时候反而安心"])

    @patch("mochi.memory_engine._append_relational_to_core")
    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_non_relational_does_not_trigger(self, mock_get_conv, mock_prompt,
                                              mock_client_fn, mock_save, mock_mark,
                                              mock_log, mock_append):
        """Non-关系 items should NOT trigger core append."""
        mock_get_conv.return_value = [
            {"id": 11, "created_at": "2025-01-01", "role": "user", "content": "I like tea"}
        ]
        mock_prompt.return_value = "Extract"
        client = _mock_client('[{"content": "喜欢喝茶", "category": "偏好", "importance": 1}]')
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        count = extract_memories(1)
        assert count == 1
        mock_append.assert_not_called()

    @patch("mochi.memory_engine._append_relational_to_core")
    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.mark_messages_processed")
    @patch("mochi.memory_engine.save_memory_item")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    @patch("mochi.memory_engine.get_unprocessed_conversations")
    def test_mixed_categories(self, mock_get_conv, mock_prompt, mock_client_fn,
                               mock_save, mock_mark, mock_log, mock_append):
        """Only 关系 items should be collected for core append."""
        mock_get_conv.return_value = [
            {"id": 12, "created_at": "2025-01-01", "role": "user", "content": "today was hard"}
        ]
        mock_prompt.return_value = "Extract"
        client = _mock_client(
            '[{"content": "养了一只猫", "category": "事实", "importance": 2},'
            ' {"content": "第一次讲述过去经历", "category": "关系", "importance": 2},'
            ' {"content": "喜欢爬山", "category": "偏好", "importance": 1}]'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        count = extract_memories(1)
        assert count == 3
        assert mock_save.call_count == 3
        mock_append.assert_called_once_with(1, ["第一次讲述过去经历"])


# ═══════════════════════════════════════════════════════════════════════════
# Integration: simulate realistic conversation → extract → verify DB state
# ═══════════════════════════════════════════════════════════════════════════

class TestRelationalIntegration:
    """End-to-end simulation: real DB, mock LLM only.

    Verifies that after extract_memories:
    - memory_items table has the correct rows
    - core_memory table has relational items auto-appended
    - dedup prevents double-write on re-extraction
    """

    def _insert_conversation(self, user_id: int, messages: list[tuple[str, str]]):
        """Insert realistic conversation messages into DB."""
        from mochi.db import save_message
        for role, content in messages:
            save_message(user_id, role, content)

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    def test_full_flow_relational_to_core(self, mock_prompt, mock_client_fn,
                                           mock_log):
        """Simulate: user has emotional conversation → LLM extracts relational
        + non-relational items → verify both memory_items and core_memory."""
        uid = 1

        # 1. Insert a realistic conversation
        self._insert_conversation(uid, [
            ("user", "今天跟你吵了一架之后感觉反而更亲近了"),
            ("assistant", "吵架有时候反而能让彼此更了解对方呢"),
            ("user", "是啊，以后心情不好的时候你别讲大道理，就陪着我就好"),
            ("assistant", "好的，记住了。心情不好的时候就安安静静陪着你"),
            ("user", "对了我最近在学弹吉他"),
        ])

        # 2. Mock LLM to return mixed categories
        mock_prompt.return_value = "Extract memories"
        llm_response = _mock_llm_response(
            '[{"content": "吵架后感觉更亲近", "category": "关系", "importance": 2},'
            ' {"content": "心情不好时不要讲大道理，陪着就好", "category": "关系", "importance": 2},'
            ' {"content": "最近在学弹吉他", "category": "目标", "importance": 1}]'
        )
        client = MagicMock()
        client.chat.return_value = llm_response
        mock_client_fn.return_value = client

        # 3. Run extraction
        from mochi.memory_engine import extract_memories
        count = extract_memories(uid)
        assert count == 3

        # 4. Verify memory_items table
        from mochi.db import get_all_memory_items
        items = get_all_memory_items(uid)
        categories = [item["category"] for item in items]
        assert "关系" in categories
        assert "目标" in categories
        relational_items = [i for i in items if i["category"] == "关系"]
        assert len(relational_items) == 2

        # 5. Verify core_memory was auto-updated
        from mochi.db import get_core_memory
        core = get_core_memory(uid)
        assert core is not None
        assert "吵架后感觉更亲近" in core
        assert "心情不好时不要讲大道理，陪着就好" in core
        # Non-relational should NOT be in core
        assert "学弹吉他" not in core

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    def test_dedup_on_second_extraction(self, mock_prompt, mock_client_fn,
                                         mock_log):
        """Run extraction twice with same relational content → core_memory
        should NOT have duplicates."""
        uid = 1

        # First round
        self._insert_conversation(uid, [
            ("user", "我们的暗号是'小饼干'"),
            ("assistant", "好的，以后小饼干就是我们的暗号啦"),
        ])
        mock_prompt.return_value = "Extract"
        client = MagicMock()
        client.chat.return_value = _mock_llm_response(
            '[{"content": "暗号是小饼干", "category": "关系", "importance": 2}]'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        extract_memories(uid)

        from mochi.db import get_core_memory
        core_after_first = get_core_memory(uid)
        assert "暗号是小饼干" in core_after_first

        # Second round — same content extracted again
        self._insert_conversation(uid, [
            ("user", "还记得我们的暗号吗"),
            ("assistant", "当然记得，小饼干！"),
        ])
        # LLM returns same relational item
        client.chat.return_value = _mock_llm_response(
            '[{"content": "暗号是小饼干", "category": "关系", "importance": 2}]'
        )
        extract_memories(uid)

        core_after_second = get_core_memory(uid)
        # Should only appear once
        assert core_after_second.count("暗号是小饼干") == 1

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    def test_token_budget_blocks_append(self, mock_prompt, mock_client_fn,
                                         mock_log, monkeypatch):
        """When core_memory is near token budget, relational items should
        go to memory_items but NOT be appended to core."""
        import mochi.memory_engine as me
        monkeypatch.setattr(me, "_RELATIONAL_TOKEN_BUDGET", 5)

        uid = 1
        # Pre-fill core_memory with content that exceeds 5 tokens
        from mochi.db import update_core_memory
        update_core_memory(uid, "This is existing core memory content that is definitely more than five tokens long")

        self._insert_conversation(uid, [
            ("user", "你记得我们的暗号吗？就是那个小饼干"),
        ])
        mock_prompt.return_value = "Extract"
        client = MagicMock()
        client.chat.return_value = _mock_llm_response(
            '[{"content": "暗号是小饼干", "category": "关系", "importance": 2}]'
        )
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        count = extract_memories(uid)
        assert count == 1

        # memory_items should still have it
        from mochi.db import get_all_memory_items
        items = get_all_memory_items(uid)
        assert any(i["category"] == "关系" for i in items)

        # But core_memory should NOT have it (budget exceeded)
        from mochi.db import get_core_memory
        core = get_core_memory(uid)
        assert "暗号是小饼干" not in core

    @patch("mochi.memory_engine.log_usage")
    @patch("mochi.memory_engine.get_client_for_tier")
    @patch("mochi.memory_engine.get_prompt")
    def test_max_cap_per_cycle(self, mock_prompt, mock_client_fn, mock_log):
        """At most 3 relational items should be appended per cycle."""
        uid = 1
        self._insert_conversation(uid, [
            ("user", "今天聊了好多心里话"),
        ])
        mock_prompt.return_value = "Extract"
        # LLM returns 5 relational items
        items_json = ','.join(
            f'{{"content": "关系记忆{i}", "category": "关系", "importance": 2}}'
            for i in range(5)
        )
        client = MagicMock()
        client.chat.return_value = _mock_llm_response(f'[{items_json}]')
        mock_client_fn.return_value = client

        from mochi.memory_engine import extract_memories
        count = extract_memories(uid)
        assert count == 5  # all 5 saved to memory_items

        # But only 3 in core_memory
        from mochi.db import get_core_memory
        core = get_core_memory(uid)
        assert core.count("- 关系记忆") == 3
