"""Tests for Anthropic message format conversion in LLM layer."""

import json
import pytest
from mochi.llm import AnthropicProvider


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
