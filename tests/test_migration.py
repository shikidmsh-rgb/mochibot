"""Tests for chat migration module.

Covers: parse_chatgpt_export, _traverse_conversation, preprocess,
_parse_llm_json, _code_density, apply_migration.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from mochi.admin.migration import (
    parse_chatgpt_export,
    _traverse_conversation,
    preprocess,
    _parse_llm_json,
    _code_density,
    estimate_context_fit,
    _sessions,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_conversation(title="Test Chat", messages=None):
    """Build a minimal ChatGPT conversation dict."""
    if messages is None:
        messages = [
            ("user", "Hello, remember I like cats"),
            ("assistant", "Got it!"),
        ]
    mapping = {}
    prev_id = None
    for i, (role, text) in enumerate(messages):
        node_id = f"node_{i}"
        mapping[node_id] = {
            "message": {
                "author": {"role": role},
                "content": {"parts": [text]},
                "create_time": 1700000000 + i,
            },
            "parent": prev_id,
            "children": [],
        }
        if prev_id:
            mapping[prev_id]["children"].append(node_id)
        prev_id = node_id
    return {"title": title, "mapping": mapping}


# ── parse_chatgpt_export ──────────────────────────────────────────────────

class TestParseChatGPTExport:

    def test_valid_json(self):
        data = [_make_conversation()]
        raw = json.dumps(data).encode()
        result = parse_chatgpt_export(raw)
        assert len(result) == 1
        assert result[0]["title"] == "Test Chat"

    def test_invalid_json(self):
        with pytest.raises(ValueError, match="无法解析"):
            parse_chatgpt_export(b"not json {{{")

    def test_not_a_list(self):
        with pytest.raises(ValueError, match="顶层应为数组"):
            parse_chatgpt_export(json.dumps({"key": "val"}).encode())

    def test_empty_list(self):
        with pytest.raises(ValueError, match="没有对话记录"):
            parse_chatgpt_export(json.dumps([]).encode())


# ── _traverse_conversation ────────────────────────────────────────────────

class TestTraverseConversation:

    def test_linear_chain(self):
        conv = _make_conversation(messages=[
            ("user", "Hello"),
            ("assistant", "Hi there"),
            ("user", "How are you?"),
        ])
        msgs = _traverse_conversation(conv["mapping"])
        assert len(msgs) == 3
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "Hello"
        assert msgs[2]["content"] == "How are you?"

    def test_empty_mapping(self):
        assert _traverse_conversation({}) == []

    def test_skips_non_string_parts(self):
        mapping = {
            "n0": {
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["text", {"image": "data"}, "more text"]},
                    "create_time": 1,
                },
                "parent": None,
                "children": [],
            }
        }
        msgs = _traverse_conversation(mapping)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "text\nmore text"


# ── _code_density ─────────────────────────────────────────────────────────

class TestCodeDensity:

    def test_no_code(self):
        assert _code_density("just plain text") == 0.0

    def test_all_code(self):
        text = "```python\nprint('hi')\n```"
        assert _code_density(text) == 1.0

    def test_mixed(self):
        text = "Some text\n```\ncode\n```\nMore text"
        density = _code_density(text)
        assert 0 < density < 1


# ── preprocess ────────────────────────────────────────────────────────────

class TestPreprocess:

    def test_basic_filtering(self):
        conversations = [_make_conversation(messages=[
            ("system", "You are a helpful assistant"),
            ("user", "Hi"),  # < 4 chars, filtered
            ("user", "Remember that I love cooking pasta"),
            ("assistant", "I'll remember that!"),
            ("tool", "search result"),
        ])]
        result = preprocess(conversations)
        assert result.conversation_count == 1
        assert result.raw_message_count == 5
        # Only the long user msg + short assistant msg should survive
        assert result.filtered_message_count == 2
        assert result.estimated_tokens > 0
        assert result.session_id in _sessions

    def test_truncates_long_assistant(self):
        long_reply = "A" * 501
        conversations = [_make_conversation(messages=[
            ("user", "Tell me about something interesting"),
            ("assistant", long_reply),
        ])]
        result = preprocess(conversations)
        # Long assistant reply is truncated, not dropped
        assert result.filtered_message_count == 2

    def test_drops_code_heavy_conversation(self):
        conversations = [_make_conversation(messages=[
            ("user", "Write a Python script for me please"),
            ("assistant", "```python\n" + "x = 1\n" * 50 + "```"),
        ])]
        result = preprocess(conversations)
        # Entire conversation dropped due to code density > 40%
        assert result.filtered_message_count == 0


# ── _parse_llm_json ───────────────────────────────────────────────────────

class TestParseLlmJson:

    def test_direct_json(self):
        data = {"soul": "friendly", "memory_items": []}
        result = _parse_llm_json(json.dumps(data))
        assert result == data

    def test_markdown_fence(self):
        text = "Here is the result:\n```json\n{\"soul\": \"kind\"}\n```"
        result = _parse_llm_json(text)
        assert result["soul"] == "kind"

    def test_embedded_json(self):
        text = "The extracted data is: {\"soul\": \"warm\"} and that's it."
        result = _parse_llm_json(text)
        assert result["soul"] == "warm"

    def test_unparseable(self):
        with pytest.raises(ValueError, match="无法解析为 JSON"):
            _parse_llm_json("no json here at all")


# ── estimate_context_fit ──────────────────────────────────────────────────

class TestEstimateContextFit:

    def test_known_model_fits(self):
        r = estimate_context_fit("gpt-4o", 50000)
        assert r["fits"] is True
        assert r["context_window"] == 128000

    def test_known_model_too_large(self):
        r = estimate_context_fit("gpt-4", 7000)
        assert r["fits"] is False
        assert r["pct"] > 0.8

    def test_unknown_model(self):
        r = estimate_context_fit("some-custom-model", 100000)
        assert r["fits"] is True
        assert r["context_window"] is None
