"""Tests for json_mode parameter on LLM providers.

Covers the framework-level JSON output guarantee added to fix router/heartbeat
JSON parse failures (markdown code-fence wrapping).

Test goals:
- OpenAI/Azure: response_format passed when json_mode=True; cached per
  (model, base_url); BadRequest triggers single retry without response_format
  and caches False; cache survives instance recreation
- Gemini: response_mime_type passed in config when json_mode=True
- Anthropic: no native param sent; markdown fence stripped only when
  json_mode=True (NOT for normal chat)
- json_mode=False is a true no-op for all providers (defaults unchanged)
"""

from unittest.mock import MagicMock, patch
import pytest

from mochi.llm import (
    OpenAIProvider, AzureOpenAIProvider, AnthropicProvider, GeminiProvider,
    _OpenAICompatChat, extract_json,
)


def _has_module(name: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(name) is not None


anthropic_required = pytest.mark.skipif(
    not _has_module("anthropic"),
    reason="anthropic SDK not installed",
)
gemini_required = pytest.mark.skipif(
    not _has_module("google.genai"),
    reason="google-genai SDK not installed",
)


# Real fence samples from gpt-5.2-chat diagnostic runs (Apr 2026, 8x8 sweep).
# Use these as test seeds to ensure framework strip handles real-world output.
REAL_GPT_FENCE_SAMPLES = [
    '```json\n{"skills":["habit"]}\n```',
    '```json\n{"skills":["todo"]}\n```',
    '```json\n{"skills":["web_search"]}\n```',
    '```json\n{"skills":["note"]}\n```',
    '```\n{"skills": []}\n```',
]


def _make_openai_response(content: str = '{"skills":[]}',
                          tool_calls=None, model: str = "gpt-test"):
    """Build a mock OpenAI ChatCompletion-like response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = "stop"
    usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    resp = MagicMock(choices=[choice], usage=usage, model=model)
    return resp


def _reset_caches():
    """Clear class-level caches between tests for isolation."""
    _OpenAICompatChat._model_caps.clear()
    _OpenAICompatChat._json_mode_caps.clear()


# ── extract_json: fence-anchor invariant + reasoning-era robustness ──────

class TestStripJsonFence:
    """Fence-anchor invariant guardian.

    These tests守护 the ^...$ anchored fence regex inside extract_json:
    fences must only be stripped when they wrap the WHOLE payload, never
    fences-as-content inside a JSON string value. Any future change that
    relaxes the anchor will break these tests + case 20 below.
    """

    @pytest.mark.parametrize("sample", REAL_GPT_FENCE_SAMPLES)
    def test_real_gpt_samples_stripped(self, sample):
        result = extract_json(sample)
        import json
        json.loads(result)

    def test_no_fence_unchanged(self):
        assert extract_json('{"a":1}') == '{"a":1}'

    def test_plain_text_returns_input(self):
        # No JSON found — extract_json returns the (stripped) input so that
        # the caller's json.loads gives a clear error including the raw.
        assert extract_json('plain text response') == 'plain text response'

    def test_empty_unchanged(self):
        assert extract_json('') == ''

    def test_fence_with_surrounding_whitespace(self):
        result = extract_json('  \n```json\n{"x":1}\n```  \n')
        import json
        assert json.loads(result) == {"x": 1}


class TestExtractJson:
    """Reasoning-era robustness — covers fence, XML wrappers, prose, edge cases."""

    def _parse(self, raw):
        import json
        return json.loads(extract_json(raw))

    # 1-2: pure JSON
    def test_pure_object(self):
        assert self._parse('{"a":1}') == {"a": 1}

    def test_pure_array(self):
        assert self._parse('[1,2,3]') == [1, 2, 3]

    # 3-4: markdown fence
    def test_fence_with_lang(self):
        assert self._parse('```json\n{"a":1}\n```') == {"a": 1}

    def test_fence_no_lang(self):
        assert self._parse('```\n{"a":1}\n```') == {"a": 1}

    # 5-6: thinking XML wrap
    def test_thinking_before(self):
        assert self._parse('<thinking>let me think</thinking>\n{"x":1}') == {"x": 1}

    def test_thinking_after(self):
        assert self._parse('{"x":1}\n<thinking>done</thinking>') == {"x": 1}

    # 7-8: natural-language prose
    def test_prose_before(self):
        assert self._parse("Sure, here's the result:\n{\"x\":1}") == {"x": 1}

    def test_prose_after(self):
        assert self._parse('{"x":1}\nLet me know if you need more.') == {"x": 1}

    # 9: fence wrapping content that contains thinking
    def test_fence_wrapping_thinking_then_json(self):
        raw = '```json\n<thinking>...</thinking>\n{"a":1}\n```'
        assert self._parse(raw) == {"a": 1}

    # 10-11: trailing commas
    def test_trailing_comma_object(self):
        assert self._parse('{"a": 1,}') == {"a": 1}

    def test_trailing_comma_array(self):
        assert self._parse('[1,2,3,]') == [1, 2, 3]

    # 12-13: empty / None
    def test_empty_string(self):
        assert extract_json("") == ""

    def test_none_input(self):
        assert extract_json(None) == ""

    # 14: completely invalid — return original (stripped) for visible error
    def test_no_json_returns_input(self):
        assert extract_json("Not a JSON at all") == "Not a JSON at all"

    # 15: nested object
    def test_nested_object(self):
        assert self._parse('{"a": {"b": [1,2,3]}}') == {"a": {"b": [1, 2, 3]}}

    # 16: regression — string contains } and {
    def test_string_value_with_braces(self):
        result = self._parse('{"msg": "} hi {"}')
        assert result == {"msg": "} hi {"}

    # 17: first-wins契约 — two adjacent JSON objects
    def test_first_wins(self):
        assert self._parse('{"a":1}{"b":2}') == {"a": 1}

    # 18: CRITICAL — XML-shaped content inside string value MUST NOT be eaten
    def test_xml_inside_string_value_preserved(self):
        raw = '{"comment": "<analysis>this is great</analysis>"}'
        result = self._parse(raw)
        assert result == {"comment": "<analysis>this is great</analysis>"}

    # 19: CRITICAL — truncated/unclosed thinking tag must not eat data
    def test_truncated_thinking_then_json(self):
        # Regex finds no </thinking>, leaves content alone; raw_decode then
        # skips the prose-y prefix and finds the JSON.
        raw = '<thinking>I was going to think but then\n{"x":1}'
        assert self._parse(raw) == {"x": 1}

    # 20: CRITICAL — fence literal inside JSON string value preserved
    # Guards the ^...$ anchor invariant on _FENCE_RE.
    def test_fence_literal_in_string_value(self):
        raw = '{"x": "```json"}'
        assert self._parse(raw) == {"x": "```json"}

    # 21: Unicode
    def test_unicode_string_value(self):
        raw = '{"name": "喜欢喝茶 ☕"}'
        assert self._parse(raw) == {"name": "喜欢喝茶 ☕"}

    # 22: deep nesting
    def test_deep_nesting(self):
        raw = '{"a":{"b":{"c":{"d":{"e":1}}}}}'
        assert self._parse(raw) == {"a": {"b": {"c": {"d": {"e": 1}}}}}

    # Bonus: analysis/reasoning/scratchpad tags also stripped
    def test_analysis_wrapper(self):
        assert self._parse('<analysis>x</analysis>{"a":1}') == {"a": 1}

    def test_reasoning_wrapper(self):
        assert self._parse('<reasoning>x</reasoning>{"a":1}') == {"a": 1}

    def test_scratchpad_wrapper(self):
        assert self._parse('<scratchpad>x</scratchpad>{"a":1}') == {"a": 1}


# ── OpenAI / Azure: response_format, cache, retry ─────────────────────────

class TestOpenAIJsonMode:
    """Verify OpenAIProvider correctly handles json_mode."""

    def setup_method(self):
        _reset_caches()

    @patch("openai.OpenAI")
    def test_json_mode_true_passes_response_format(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            '{"skills":["weather"]}'
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert call_kwargs.get("response_format") == {"type": "json_object"}

    @patch("openai.OpenAI")
    def test_json_mode_false_omits_response_format(self, mock_openai_cls):
        """Default json_mode=False must not introduce new behavior."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response()

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        provider.chat([{"role": "user", "content": "hi"}])  # default

        call_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "response_format" not in call_kwargs

    @patch("openai.OpenAI")
    def test_strip_fence_applied_when_json_mode(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response(
            '```json\n{"skills":["habit"]}\n```'
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}],
                               json_mode=True)
        assert result.content == '{"skills":["habit"]}'

    @patch("openai.OpenAI")
    def test_no_strip_when_json_mode_false(self, mock_openai_cls):
        """Normal chat with fenced code block must NOT be stripped."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        fenced_code = "Here is the answer:\n```python\nprint('hi')\n```"
        mock_client.chat.completions.create.return_value = _make_openai_response(
            fenced_code
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}])  # no json_mode
        assert result.content == fenced_code

    @patch("openai.OpenAI")
    def test_bad_request_falls_back_and_caches(self, mock_openai_cls):
        """Server returning 400 on response_format → drop it, retry, cache False."""
        from openai import BadRequestError
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # First call raises BadRequest, second (retry) succeeds.
        bad_request = BadRequestError(
            "unknown parameter response_format",
            response=MagicMock(), body=None,
        )
        mock_client.chat.completions.create.side_effect = [
            bad_request,
            _make_openai_response(),
        ]

        provider = OpenAIProvider(api_key="k", model="legacy-model")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        # Two calls happened (first failed, retry succeeded)
        assert mock_client.chat.completions.create.call_count == 2
        # Retry kwargs must NOT include response_format
        retry_kwargs = mock_client.chat.completions.create.call_args_list[1].kwargs
        assert "response_format" not in retry_kwargs
        # Cache marked unsupported for (model, base_url=)
        assert _OpenAICompatChat._json_mode_caps.get(("legacy-model", "")) is False

    @patch("openai.OpenAI")
    def test_cached_unsupported_skips_response_format(self, mock_openai_cls):
        """Once cached as False, subsequent calls don't try response_format."""
        # Pre-seed cache
        _OpenAICompatChat._json_mode_caps[("legacy-model", "")] = False

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response()

        provider = OpenAIProvider(api_key="k", model="legacy-model")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        # Single call, no response_format
        assert mock_client.chat.completions.create.call_count == 1
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "response_format" not in kwargs

    @patch("openai.OpenAI")
    def test_cache_keyed_by_model_and_base_url(self, mock_openai_cls):
        """Same model on different base_url must be cached independently."""
        # Seed: model "shared" is unsupported on endpoint A
        _OpenAICompatChat._json_mode_caps[("shared", "https://endpoint-a/")] = False

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _make_openai_response()

        # Provider on endpoint B should NOT inherit endpoint A's cached False
        provider = OpenAIProvider(api_key="k", model="shared",
                                  base_url="https://endpoint-b/")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs.get("response_format") == {"type": "json_object"}

    @patch("openai.OpenAI")
    def test_cache_survives_provider_recreation(self, mock_openai_cls):
        """Class-level cache means a fresh provider instance reuses learned caps."""
        from openai import BadRequestError

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        bad_request = BadRequestError("nope", response=MagicMock(), body=None)
        mock_client.chat.completions.create.side_effect = [
            bad_request,
            _make_openai_response(),
        ]

        # First instance: triggers fallback + caches False
        provider1 = OpenAIProvider(api_key="k", model="legacy")
        provider1.chat([{"role": "user", "content": "hi"}], json_mode=True)
        first_call_count = mock_client.chat.completions.create.call_count
        assert first_call_count == 2  # initial + retry

        # Second instance with same (model, base_url): goes straight to no-RF path
        mock_client.chat.completions.create.side_effect = None
        mock_client.chat.completions.create.return_value = _make_openai_response()
        provider2 = OpenAIProvider(api_key="k", model="legacy")
        provider2.chat([{"role": "user", "content": "hi"}], json_mode=True)

        # Only one new call (no retry)
        assert mock_client.chat.completions.create.call_count == first_call_count + 1
        last_kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert "response_format" not in last_kwargs


# ── Gemini ────────────────────────────────────────────────────────────────

@gemini_required
class TestGeminiJsonMode:

    def setup_method(self):
        _reset_caches()

    def _make_gemini_response(self, text: str):
        part = MagicMock()
        part.text = text
        part.function_call = None
        content_obj = MagicMock(parts=[part])
        candidate = MagicMock(content=content_obj, finish_reason=MagicMock(name="STOP"))
        candidate.finish_reason.name = "STOP"
        usage = MagicMock(prompt_token_count=5, candidates_token_count=3)
        resp = MagicMock(candidates=[candidate], usage_metadata=usage)
        return resp

    @patch("google.genai.Client")
    def test_json_mode_sets_response_mime_type(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response(
            '{"skills":[]}'
        )

        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        # Config object built from GenerateContentConfig — inspect via attr access
        assert getattr(config, "response_mime_type", None) == "application/json"

    @patch("google.genai.Client")
    def test_json_mode_false_no_mime_type(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response("hi")

        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        provider.chat([{"role": "user", "content": "hi"}])

        call_kwargs = mock_client.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert getattr(config, "response_mime_type", None) is None

    @patch("google.genai.Client")
    def test_gemini_strips_fence_when_json_mode(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response(
            '```json\n{"skills":["weather"]}\n```'
        )

        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        result = provider.chat([{"role": "user", "content": "hi"}], json_mode=True)
        assert result.content == '{"skills":["weather"]}'


# ── Anthropic ─────────────────────────────────────────────────────────────

@anthropic_required
class TestAnthropicJsonMode:

    def _make_anthropic_response(self, text: str):
        block = MagicMock()
        block.type = "text"
        block.text = text
        usage = MagicMock(input_tokens=10, output_tokens=5)
        resp = MagicMock(content=[block], usage=usage, stop_reason="end_turn")
        return resp

    @patch("anthropic.Anthropic")
    def test_anthropic_json_mode_strips_fence(self, mock_anthropic_cls):
        """Anthropic has no native JSON mode; framework strip is the only fix."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._make_anthropic_response(
            '```json\n{"skills":["habit"]}\n```'
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}], json_mode=True)
        assert result.content == '{"skills":["habit"]}'

    @patch("anthropic.Anthropic")
    def test_anthropic_no_strip_when_json_mode_false(self, mock_anthropic_cls):
        """CRITICAL: Normal Claude chat with code fences must NOT be corrupted."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        legitimate_code_response = (
            "Here's the function:\n```python\ndef foo():\n    return 42\n```"
        )
        mock_client.messages.create.return_value = self._make_anthropic_response(
            legitimate_code_response
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])  # default
        assert result.content == legitimate_code_response

    @patch("anthropic.Anthropic")
    def test_anthropic_does_not_send_native_json_param(self, mock_anthropic_cls):
        """Anthropic API has no JSON mode field; we must not invent one."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._make_anthropic_response("{}")

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        provider.chat([{"role": "user", "content": "hi"}], json_mode=True)

        kwargs = mock_client.messages.create.call_args.kwargs
        # No spurious JSON-mode-ish keys
        assert "response_format" not in kwargs
        assert "response_mime_type" not in kwargs
