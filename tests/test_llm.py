"""Tests for Anthropic message format conversion in LLM layer."""

import json
import pytest
from unittest.mock import MagicMock, patch
from mochi.llm import AnthropicProvider, OpenAIProvider, _OpenAICompatChat


class TestAnthropicConvertMessages:
    """Test that OpenAI-format tool messages convert correctly to Anthropic format."""

    def test_plain_messages_unchanged(self):
        msgs = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = AnthropicProvider._convert_messages(msgs)
        assert result == msgs

    def test_tool_call_conversion(self):
        msgs = [
            {"role": "user", "content": "Set a reminder"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "manage_reminder",
                            "arguments": json.dumps({"action": "create", "message": "Test"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "Reminder created!",
            },
        ]
        result = AnthropicProvider._convert_messages(msgs)

        assert len(result) == 3
        # User message unchanged
        assert result[0] == {"role": "user", "content": "Set a reminder"}
        # Assistant message converted to content blocks
        assert result[1]["role"] == "assistant"
        blocks = result[1]["content"]
        assert any(b["type"] == "tool_use" and b["id"] == "call_123" for b in blocks)
        # Tool result as user message
        assert result[2]["role"] == "user"
        tool_results = result[2]["content"]
        assert tool_results[0]["type"] == "tool_result"
        assert tool_results[0]["tool_use_id"] == "call_123"

    def test_multiple_tool_calls(self):
        msgs = [
            {
                "role": "assistant",
                "content": "Let me check both.",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "tool_a", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "tool_b", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "result a"},
            {"role": "tool", "tool_call_id": "call_b", "content": "result b"},
        ]
        result = AnthropicProvider._convert_messages(msgs)

        # Assistant has 3 blocks: text + 2 tool_use
        assistant_blocks = result[0]["content"]
        assert len(assistant_blocks) == 3
        assert assistant_blocks[0]["type"] == "text"
        assert assistant_blocks[1]["type"] == "tool_use"
        assert assistant_blocks[2]["type"] == "tool_use"

        # Both tool results merged into one user message
        assert result[1]["role"] == "user"
        assert len(result[1]["content"]) == 2

    def test_convert_tools_format(self):
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": ["x"],
                    },
                },
            }
        ]
        anthropic_tools = AnthropicProvider._convert_tools(openai_tools)
        assert len(anthropic_tools) == 1
        t = anthropic_tools[0]
        assert t["name"] == "test_tool"
        assert t["description"] == "A test tool"
        assert "input_schema" in t


class TestCapsCache:
    """Test that model capability flags survive provider instance recreation."""

    def setup_method(self):
        # Clear class-level cache before each test
        _OpenAICompatChat._model_caps.clear()

    def _make_mock_response(self):
        """Create a minimal mock OpenAI chat completion response."""
        msg = MagicMock()
        msg.content = "hi"
        msg.tool_calls = None
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        usage = MagicMock()
        usage.prompt_tokens = 10
        usage.completion_tokens = 5
        usage.total_tokens = 15
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = usage
        return resp

    @patch("openai.OpenAI")
    def test_caps_restored_from_cache(self, MockOpenAI):
        """Second instance for same model gets caps from class cache."""
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        from openai import BadRequestError

        # First call: 400 on temperature → retry succeeds
        mock_client.chat.completions.create.side_effect = [
            BadRequestError(
                message="temperature is not supported for this model",
                response=MagicMock(status_code=400),
                body=None,
            ),
            self._make_mock_response(),
        ]

        p1 = OpenAIProvider(api_key="k", model="no-temp-model")
        p1.chat([{"role": "user", "content": "hi"}])

        assert p1._use_temperature is False
        assert "no-temp-model" in _OpenAICompatChat._model_caps

        # Second instance — should inherit from cache, no retry needed
        mock_client.chat.completions.create.reset_mock()
        mock_client.chat.completions.create.side_effect = [
            self._make_mock_response(),
        ]

        p2 = OpenAIProvider(api_key="k", model="no-temp-model")
        assert p2._use_temperature is False  # pre-populated from cache

        p2.chat([{"role": "user", "content": "hello"}])

        # Verify temperature was NOT in the kwargs
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "temperature" not in call_kwargs

    @patch("openai.OpenAI")
    def test_new_model_still_probes(self, MockOpenAI):
        """A model not in cache still goes through normal negotiation."""
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._make_mock_response()

        p = OpenAIProvider(api_key="k", model="brand-new-model")
        assert p._use_temperature is None  # not in cache

        p.chat([{"role": "user", "content": "hi"}])
        assert p._use_temperature is True  # learned from success

        # Now cached
        assert "brand-new-model" in _OpenAICompatChat._model_caps
        assert _OpenAICompatChat._model_caps["brand-new-model"]["use_temperature"] is True
