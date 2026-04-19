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

    # ── Thinking config (Gemini 3 thinking_level / 2.5 thinking_budget) ──

    @patch("google.genai.Client")
    def test_gemini_3_sends_thinking_level_low(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response("ok")

        provider = GeminiProvider(api_key="k", model="gemini-3-pro-preview")
        provider.chat([{"role": "user", "content": "hi"}])

        cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        tc = getattr(cfg, "thinking_config", None)
        assert tc is not None
        # SDK normalizes "low" → ThinkingLevel.LOW enum
        assert str(getattr(tc, "thinking_level", "")).lower().endswith("low")

    @patch("google.genai.Client")
    def test_gemini_25_sends_thinking_budget(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response("ok")

        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        provider.chat([{"role": "user", "content": "hi"}])

        cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        tc = getattr(cfg, "thinking_config", None)
        assert tc is not None
        assert getattr(tc, "thinking_budget", None) == 512

    @patch("google.genai.Client")
    def test_gemini_legacy_no_thinking_config(self, mock_genai_cls):
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        mock_client.models.generate_content.return_value = self._make_gemini_response("ok")

        provider = GeminiProvider(api_key="k", model="gemini-1.5-flash")
        provider.chat([{"role": "user", "content": "hi"}])

        cfg = mock_client.models.generate_content.call_args.kwargs["config"]
        # Legacy models don't get thinking_config at all
        assert getattr(cfg, "thinking_config", None) is None


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


# ── reasoning_tokens / cached_prompt_tokens parsing ──────────────────────

class TestReasoningTokensParsing:
    """Verify _openai_response correctly extracts token detail fields."""

    def _build_resp(self, comp_details=None, prompt_details=None):
        """Build a mock OpenAI response with controllable usage details."""
        from types import SimpleNamespace
        msg = MagicMock()
        msg.content = "ok"
        msg.tool_calls = None
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        # Use SimpleNamespace so getattr returns None for missing attrs
        # (MagicMock would auto-create them, breaking our None checks).
        usage = SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            completion_tokens_details=comp_details,
            prompt_tokens_details=prompt_details,
        )
        return MagicMock(choices=[choice], usage=usage, model="gpt-test")

    def setup_method(self):
        _reset_caches()

    @patch("openai.OpenAI")
    def test_reasoning_tokens_parsed(self, mock_openai_cls):
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._build_resp(
            comp_details=SimpleNamespace(reasoning_tokens=42),
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.reasoning_tokens == 42

    @patch("openai.OpenAI")
    def test_no_details_returns_none(self, mock_openai_cls):
        """Old SDK / non-reasoning model: no details object → None (not 0)."""
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._build_resp(
            comp_details=None, prompt_details=None,
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.reasoning_tokens is None
        assert result.cached_prompt_tokens is None

    @patch("openai.OpenAI")
    def test_explicit_zero_preserved(self, mock_openai_cls):
        """reasoning_tokens=0 from SDK must NOT collapse to None."""
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._build_resp(
            comp_details=SimpleNamespace(reasoning_tokens=0),
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.reasoning_tokens == 0  # not None

    @patch("openai.OpenAI")
    def test_cached_prompt_tokens_parsed(self, mock_openai_cls):
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = self._build_resp(
            prompt_details=SimpleNamespace(cached_tokens=100),
        )

        provider = OpenAIProvider(api_key="k", model="gpt-test")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.cached_prompt_tokens == 100

    @gemini_required
    @patch("google.genai.Client")
    def test_gemini_does_not_set_reasoning_fields(self, mock_genai_cls):
        """Non-OpenAI providers must leave reasoning_tokens at default None."""
        mock_client = MagicMock()
        mock_genai_cls.return_value = mock_client
        # Reuse the gemini test response builder shape inline
        part = MagicMock()
        part.text = "ok"
        part.function_call = None
        content_obj = MagicMock(parts=[part])
        candidate = MagicMock(content=content_obj)
        candidate.finish_reason.name = "STOP"
        usage = MagicMock(prompt_token_count=5, candidates_token_count=3)
        mock_client.models.generate_content.return_value = MagicMock(
            candidates=[candidate], usage_metadata=usage,
        )

        provider = GeminiProvider(api_key="k", model="gemini-2.5-flash")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.reasoning_tokens is None
        assert result.cached_prompt_tokens is None

    @anthropic_required
    @patch("anthropic.Anthropic")
    def test_anthropic_does_not_set_reasoning_fields(self, mock_anthropic_cls):
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # SimpleNamespace (not MagicMock) so missing cache_* attrs stay missing.
        block = SimpleNamespace(type="text", text="ok")
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = SimpleNamespace(
            content=[block], usage=usage, stop_reason="end_turn",
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.reasoning_tokens is None
        assert result.cached_prompt_tokens is None


# ── Anthropic prompt caching + extended thinking (P0-1 + P0-2) ───────────

@anthropic_required
class TestAnthropicCachingAndThinking:
    """Verify AnthropicProvider sends cache_control + thinking correctly,
    filters thinking blocks out of content, and parses cache usage fields.
    """

    def _build_resp(self, blocks, usage=None):
        """Build a mock anthropic response. Blocks must be SimpleNamespace
        objects (not MagicMock) so `block.type` checks work and missing
        attributes don't auto-create."""
        from types import SimpleNamespace
        if usage is None:
            usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        return SimpleNamespace(
            content=blocks, usage=usage, stop_reason="end_turn",
        )

    def _text_block(self, text):
        from types import SimpleNamespace
        return SimpleNamespace(type="text", text=text)

    def _tool_block(self, id, name, input):
        from types import SimpleNamespace
        return SimpleNamespace(type="tool_use", id=id, name=name, input=input)

    def _thinking_block(self, text):
        from types import SimpleNamespace
        return SimpleNamespace(type="thinking", thinking=text)

    def _redacted_thinking_block(self, data):
        from types import SimpleNamespace
        return SimpleNamespace(type="redacted_thinking", data=data)

    # 1: caching system block format
    @patch("anthropic.Anthropic")
    def test_system_uses_list_with_cache_control(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._build_resp(
            [self._text_block("ok")],
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        provider.chat([
            {"role": "system", "content": "You are Mochi."},
            {"role": "user", "content": "hi"},
        ])

        kwargs = mock_client.messages.create.call_args.kwargs
        system = kwargs.get("system")
        assert isinstance(system, list), f"expected list, got {type(system)}"
        assert len(system) >= 1
        first = system[0]
        assert first["type"] == "text"
        assert first["text"] == "You are Mochi."
        assert first.get("cache_control") == {"type": "ephemeral"}

    # 2: thinking enabled for Claude 4.x
    @pytest.mark.parametrize("model", [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
    ])
    @patch("anthropic.Anthropic")
    def test_thinking_enabled_for_claude_4(self, mock_anthropic_cls, model):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._build_resp(
            [self._text_block("ok")],
        )

        provider = AnthropicProvider(api_key="k", model=model)
        provider.chat([{"role": "user", "content": "hi"}])

        kwargs = mock_client.messages.create.call_args.kwargs
        thinking = kwargs.get("thinking")
        assert thinking is not None, f"thinking missing for {model}"
        assert thinking.get("type") == "enabled"
        assert thinking.get("budget_tokens") == 1024

    # 3: thinking NOT sent for legacy models (B1 guard — date stamps with 4 in them)
    @pytest.mark.parametrize("model", [
        "claude-3-haiku-20240307",     # date contains "4" but not "-4-"
        "claude-3-5-sonnet-20241022",
        "claude-3-opus-20240229",
        "claude-3-5-haiku-20241022",
    ])
    @patch("anthropic.Anthropic")
    def test_thinking_NOT_sent_for_legacy_models(self, mock_anthropic_cls, model):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._build_resp(
            [self._text_block("ok")],
        )

        provider = AnthropicProvider(api_key="k", model=model)
        provider.chat([{"role": "user", "content": "hi"}])

        kwargs = mock_client.messages.create.call_args.kwargs
        assert "thinking" not in kwargs, (
            f"REGRESSION: thinking sent to legacy model {model} — "
            "B1 detection bug has returned"
        )

    # 4: thinking block must NOT leak into content
    @patch("anthropic.Anthropic")
    def test_thinking_block_filtered_from_content(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._build_resp([
            self._thinking_block("internal reasoning here"),
            self._text_block("final answer"),
        ])

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.content == "final answer"
        assert "internal reasoning" not in result.content

    # 5: redacted_thinking block must also NOT leak (I4 guard)
    @patch("anthropic.Anthropic")
    def test_redacted_thinking_block_filtered(self, mock_anthropic_cls):
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = self._build_resp([
            self._redacted_thinking_block("encrypted-blob"),
            self._text_block("real response"),
        ])

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.content == "real response"
        assert "encrypted-blob" not in result.content

    # 6: cached_prompt_tokens parsed when usage exposes cache_read_input_tokens
    @patch("anthropic.Anthropic")
    def test_cached_prompt_tokens_parsed(self, mock_anthropic_cls):
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        usage = SimpleNamespace(
            input_tokens=10, output_tokens=5,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=0,
        )
        mock_client.messages.create.return_value = self._build_resp(
            [self._text_block("ok")], usage=usage,
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.cached_prompt_tokens == 500

    # 7: cache fields absent → None (B2 guard — must not become 0 or MagicMock)
    @patch("anthropic.Anthropic")
    def test_cache_fields_absent_returns_none(self, mock_anthropic_cls):
        from types import SimpleNamespace
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # Old SDK: usage object has no cache_* attributes at all
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = self._build_resp(
            [self._text_block("ok")], usage=usage,
        )

        provider = AnthropicProvider(api_key="k", model="claude-haiku-4-5")
        result = provider.chat([{"role": "user", "content": "hi"}])
        assert result.cached_prompt_tokens is None
        assert result.reasoning_tokens is None
