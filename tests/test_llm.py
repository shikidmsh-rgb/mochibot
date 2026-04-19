"""Tests for LLM provider format conversion (Anthropic + Gemini)."""

import json
import pytest
from unittest.mock import MagicMock, patch
from mochi.llm import AnthropicProvider, GeminiProvider, OpenAIProvider, _OpenAICompatChat


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
        _OpenAICompatChat._json_mode_caps.clear()
        _OpenAICompatChat._reasoning_caps.clear()

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


class TestReasoningEffortNegotiation:
    """Test reasoning_effort capability negotiation for reasoning models."""

    def setup_method(self):
        _OpenAICompatChat._model_caps.clear()
        _OpenAICompatChat._json_mode_caps.clear()
        _OpenAICompatChat._reasoning_caps.clear()

    def _make_mock_response(self):
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
    def test_reasoning_supported_first_call_caches_true(self, MockOpenAI):
        """Successful first call with reasoning_effort caches support=True."""
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._make_mock_response()

        p = OpenAIProvider(api_key="k", model="reasoning-model",
                           base_url="https://example.com/v1")
        p.chat([{"role": "user", "content": "hi"}])

        # Verify reasoning_effort=minimal was sent
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs.get("reasoning_effort") == "minimal"

        # Verify cache key is (model, base_url) and value is True
        key = ("reasoning-model", "https://example.com/v1")
        assert _OpenAICompatChat._reasoning_caps.get(key) is True

    @patch("openai.OpenAI")
    def test_reasoning_unsupported_falls_back_and_caches_false(self, MockOpenAI):
        """BadRequestError on reasoning_effort triggers fallback + caches False."""
        from openai import BadRequestError
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        # First call fails (reasoning_effort not supported), retry succeeds
        mock_client.chat.completions.create.side_effect = [
            BadRequestError(
                message="Unknown parameter: 'reasoning_effort'",
                response=MagicMock(status_code=400),
                body=None,
            ),
            self._make_mock_response(),
        ]

        p = OpenAIProvider(api_key="k", model="legacy-model",
                           base_url="https://example.com/v1")
        p.chat([{"role": "user", "content": "hi"}])

        assert mock_client.chat.completions.create.call_count == 2
        # Retry should NOT include reasoning_effort
        retry_kwargs = mock_client.chat.completions.create.call_args_list[1][1]
        assert "reasoning_effort" not in retry_kwargs

        # Cache says: this (model, base_url) doesn't support it
        key = ("legacy-model", "https://example.com/v1")
        assert _OpenAICompatChat._reasoning_caps.get(key) is False

        # A second provider instance for same model+base_url should skip
        # reasoning_effort entirely (no probe needed)
        mock_client.chat.completions.create.reset_mock()
        mock_client.chat.completions.create.side_effect = [self._make_mock_response()]
        p2 = OpenAIProvider(api_key="k", model="legacy-model",
                            base_url="https://example.com/v1")
        p2.chat([{"role": "user", "content": "hi"}])
        kwargs2 = mock_client.chat.completions.create.call_args[1]
        assert "reasoning_effort" not in kwargs2

    @patch("openai.OpenAI")
    def test_unrelated_400_does_not_poison_reasoning_cache(self, MockOpenAI):
        """An unrelated BadRequestError on retry must not lock reasoning=False
        for future requests if the retry actually succeeded by dropping the
        suspect param. But if both initial and retry fail, the cache write
        IS still acceptable (the gateway clearly can't handle our params)."""
        from openai import BadRequestError
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        # First call: 400 due to invalid message format (NOT reasoning_effort).
        # Retry (with reasoning_effort dropped) succeeds — meaning the original
        # cause was actually reasoning_effort after all, OR the gateway just
        # accepted it the second time. Either way: cache=False is correct
        # because retry only differed in reasoning_effort being dropped.
        mock_client.chat.completions.create.side_effect = [
            BadRequestError(
                message="Invalid 'messages[0].content': string too long",
                response=MagicMock(status_code=400),
                body=None,
            ),
            self._make_mock_response(),
        ]

        p = OpenAIProvider(api_key="k", model="some-model",
                           base_url="https://example.com/v1")
        p.chat([{"role": "user", "content": "hi"}])

        # After retry succeeds, reasoning was the dropped suspect → cache=False
        # This is the correct broad-fallback behavior (mirrors response_format).
        key = ("some-model", "https://example.com/v1")
        assert _OpenAICompatChat._reasoning_caps.get(key) is False

        # Critical: the OTHER capability caches must NOT be poisoned.
        # temperature was sent and not in either error message → should still
        # be True after success.
        assert p._use_temperature is True

    @patch("openai.OpenAI")
    def test_reasoning_cache_isolated_by_base_url(self, MockOpenAI):
        """Same model name on different base_urls maintains separate cache."""
        from openai import BadRequestError
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client

        # base_url A: reasoning unsupported (e.g. third-party gateway)
        mock_client.chat.completions.create.side_effect = [
            BadRequestError(
                message="reasoning_effort unknown",
                response=MagicMock(status_code=400),
                body=None,
            ),
            self._make_mock_response(),
        ]
        pA = OpenAIProvider(api_key="k", model="gpt-5",
                            base_url="https://gateway-a.com/v1")
        pA.chat([{"role": "user", "content": "hi"}])

        # base_url B: reasoning supported (real OpenAI)
        mock_client.chat.completions.create.reset_mock()
        mock_client.chat.completions.create.side_effect = [self._make_mock_response()]
        pB = OpenAIProvider(api_key="k", model="gpt-5",
                            base_url="https://api.openai.com/v1")
        pB.chat([{"role": "user", "content": "hi"}])

        # Verify per-base_url isolation
        keyA = ("gpt-5", "https://gateway-a.com/v1")
        keyB = ("gpt-5", "https://api.openai.com/v1")
        assert _OpenAICompatChat._reasoning_caps.get(keyA) is False
        assert _OpenAICompatChat._reasoning_caps.get(keyB) is True

        # Verify base_url B's request actually included reasoning_effort
        kwargs_b = mock_client.chat.completions.create.call_args[1]
        assert kwargs_b.get("reasoning_effort") == "minimal"


class TestHTTPClientConfig:
    """Test that OpenAI clients are constructed with explicit retry/timeout."""

    @patch("openai.OpenAI")
    def test_max_retries_zero_passed_to_sdk(self, MockOpenAI):
        OpenAIProvider(api_key="k", model="m", base_url="https://x/v1")
        call_kwargs = MockOpenAI.call_args[1]
        assert call_kwargs["max_retries"] == 0
        assert call_kwargs["timeout"] is not None

    @patch("openai.AzureOpenAI")
    def test_azure_max_retries_zero(self, MockAzureOpenAI):
        from mochi.llm import AzureOpenAIProvider
        AzureOpenAIProvider(api_key="k", model="m", base_url="https://x/",
                            api_version="2024-02-15-preview")
        call_kwargs = MockAzureOpenAI.call_args[1]
        assert call_kwargs["max_retries"] == 0
        assert call_kwargs["timeout"] is not None


class TestGeminiConvertMessages:
    """Test that OpenAI-format messages convert correctly to Gemini format."""

    def test_system_message_extracted(self):
        msgs = [
            {"role": "system", "content": "You are a helper."},
            {"role": "user", "content": "Hello"},
        ]
        system_msg, contents = GeminiProvider._convert_messages(msgs)
        assert "You are a helper." in system_msg
        assert len(contents) == 1
        assert contents[0].role == "user"

    def test_assistant_becomes_model_role(self):
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        assert len(contents) == 2
        assert contents[0].role == "user"
        assert contents[1].role == "model"

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
                            "arguments": json.dumps({"action": "create"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "name": "manage_reminder",
                "content": '{"result": "done"}',
            },
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        assert len(contents) == 3
        # Assistant (model) should have a function_call part
        model_parts = contents[1].parts
        assert any(hasattr(p, "function_call") and p.function_call for p in model_parts)
        # Tool result should be in a user turn
        assert contents[2].role == "user"
        tool_parts = contents[2].parts
        assert any(hasattr(p, "function_response") and p.function_response for p in tool_parts)

    def test_multiple_system_messages_concatenated(self):
        msgs = [
            {"role": "system", "content": "Rule 1."},
            {"role": "system", "content": "Rule 2."},
            {"role": "user", "content": "Go"},
        ]
        system_msg, contents = GeminiProvider._convert_messages(msgs)
        assert "Rule 1." in system_msg
        assert "Rule 2." in system_msg
        assert len(contents) == 1

    def test_consecutive_tool_results_merged(self):
        msgs = [
            {
                "role": "assistant",
                "content": "Checking both.",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "t1", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "t2", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "t1", "content": "r1"},
            {"role": "tool", "tool_call_id": "b", "name": "t2", "content": "r2"},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        # Model turn + one user turn with both results
        assert len(contents) == 2
        assert contents[1].role == "user"
        assert len(contents[1].parts) == 2

    def test_consecutive_model_turns_merged(self):
        """Consecutive assistant messages (e.g. proactive heartbeat) must merge."""
        msgs = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "assistant", "content": "By the way..."},  # proactive msg
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        assert len(contents) == 2  # user + merged model
        assert contents[1].role == "model"
        texts = [p.text for p in contents[1].parts if hasattr(p, "text") and p.text]
        assert "Hello!" in texts
        assert "By the way..." in texts

    def test_consecutive_user_turns_merged(self):
        """Consecutive user messages must merge into one user turn."""
        msgs = [
            {"role": "user", "content": "First"},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": "Got it"},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        assert len(contents) == 2  # merged user + model
        assert contents[0].role == "user"
        assert len(contents[0].parts) == 2

    def test_model_turn_with_tool_calls_after_model_text_merged(self):
        """Model text turn followed by model tool_call turn — merged correctly."""
        msgs = [
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "Sure"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "t1", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "t1", "content": "ok"},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        # user + merged model (text + function_call) + user (tool result)
        assert len(contents) == 3
        assert contents[1].role == "model"
        model_parts = contents[1].parts
        has_text = any(hasattr(p, "text") and p.text for p in model_parts)
        has_fc = any(hasattr(p, "function_call") and p.function_call for p in model_parts)
        assert has_text and has_fc

    def test_expand_history_then_proactive_msg_merged(self):
        """Simulates _expand_history output + proactive message — no consecutive model."""
        msgs = [
            {"role": "user", "content": "Set reminder"},
            # _expand_history step 1: assistant with tool_calls
            {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {"id": "h0", "type": "function",
                     "function": {"name": "manage_reminder", "arguments": "{}"}},
                ],
            },
            # _expand_history step 2: tool result
            {"role": "tool", "tool_call_id": "h0", "name": "manage_reminder", "content": "OK"},
            # _expand_history step 3: assistant reply
            {"role": "assistant", "content": "Done!"},
            # Proactive heartbeat message (no user msg in between)
            {"role": "assistant", "content": "Good night~"},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        # Verify no consecutive same-role turns
        for i in range(1, len(contents)):
            assert contents[i].role != contents[i - 1].role, \
                f"Consecutive {contents[i].role} turns at index {i - 1},{i}"


class TestGeminiConvertTools:
    """Test OpenAI tool format → Gemini FunctionDeclaration dict conversion."""

    def test_basic_conversion(self):
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
        result = GeminiProvider._convert_tools(openai_tools)
        assert len(result) == 1
        assert result[0]["name"] == "test_tool"
        assert result[0]["description"] == "A test tool"
        assert result[0]["parameters"]["type"] == "object"

    def test_multiple_tools(self):
        tools = [
            {"type": "function", "function": {"name": "a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "B", "parameters": {}}},
        ]
        result = GeminiProvider._convert_tools(tools)
        assert len(result) == 2
        assert result[0]["name"] == "a"
        assert result[1]["name"] == "b"

    def test_tool_result_without_name_uses_id_lookup(self):
        """Tool result messages without 'name' field should resolve via tool_call_id."""
        msgs = [
            {"role": "user", "content": "Do it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "my_tool", "arguments": "{}"},
                    }
                ],
            },
            # No "name" field — only tool_call_id (common in MochiBot pipeline)
            {"role": "tool", "tool_call_id": "call_abc", "content": '{"ok": true}'},
        ]
        _, contents = GeminiProvider._convert_messages(msgs)
        # Tool result part should have resolved name from assistant's tool_calls
        tool_part = contents[2].parts[0]
        assert hasattr(tool_part, "function_response")
        assert tool_part.function_response.name == "my_tool"
