"""LLM provider abstraction — provider-agnostic.

Supports any OpenAI-compatible API, Azure OpenAI, Anthropic, and Google Gemini.

Usage:
    from mochi.llm import get_client_for_tier
    client = get_client_for_tier()         # chat tier (default)
    client = get_client_for_tier("deep")   # deep tier
    response = client.chat(messages, tools=...)
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypedDict

import httpx

from mochi.config import (
    CHAT_PROVIDER, CHAT_API_KEY, CHAT_MODEL, CHAT_BASE_URL,
    THINK_PROVIDER, THINK_API_KEY, THINK_MODEL, THINK_BASE_URL,
    AZURE_API_VERSION,
)

log = logging.getLogger(__name__)

# Explicit timeout for OpenAI-compatible HTTP clients. SDK default is 600s read,
# which silently masks slow gateways. Read=120s is well above worst-case
# reasoning-model latency on slow third-party gateways but fails fast on hangs.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


class ToolCallDict(TypedDict):
    """Typed structure for a single tool call in LLMResponse."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    content: str = ""
    tool_calls: list[ToolCallDict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    # None = SDK didn't report (legacy SDK / non-reasoning model / non-OpenAI
    # provider). 0 = model explicitly reported zero. The distinction matters
    # for cost telemetry — see plan P1-2.
    reasoning_tokens: int | None = None
    cached_prompt_tokens: int | None = None


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 1.0, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        """Send a chat completion request.

        json_mode=True asks the provider to return strict JSON. Each provider
        maps this to its native capability (response_format / response_mime_type).
        Anthropic has no native JSON mode — caller must rely on prompting plus
        the framework-layer markdown fence strip.
        """
        ...

    @abstractmethod
    def provider_name(self) -> str:
        ...


# Anchored fence matcher. The ^...$ anchors are an INVARIANT: they prevent
# matching fences that appear inside JSON string values (e.g. {"x": "```json"}).
# Do not relax to a non-anchored search — see TestStripJsonFence + case 20.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

# Reasoning-model wrappers some models emit around (or instead of) JSON.
# Paired non-greedy match: a TRUNCATED tag (no closing) WILL NOT match,
# which is intentional — better to leave content alone than risk eating
# real JSON because the closing tag is missing.
_REASONING_XML_RE = re.compile(
    r"<(thinking|analysis|reasoning|scratchpad)>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# Trailing comma before } or ] — a common LLM JSON defect.
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _try_extract(s: str) -> str | None:
    """Find the first complete JSON object/array in s using stdlib raw_decode.

    Returns the JSON substring on success, None if no parseable JSON found.
    O(n²) worst case — do not call on >100KB inputs (LLM JSON < 10KB in
    practice). One trailing-comma fixup retry per candidate position.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        try:
            _, end = decoder.raw_decode(s[i:])
            return s[i:i + end]
        except json.JSONDecodeError:
            chunk = s[i:]
            fixed = _TRAILING_COMMA_RE.sub(r"\1", chunk)
            if fixed != chunk:
                try:
                    _, end = decoder.raw_decode(fixed)
                    return fixed[:end]
                except json.JSONDecodeError:
                    pass
            continue
    return None


def extract_json(content: str) -> str:
    """Extract the first complete JSON object/array from a string.

    Handles four real-world failure modes from reasoning-era LLMs:
      1. Markdown fence wrap: ```json\\n{...}\\n```
      2. Reasoning XML wrap: <thinking>...</thinking>{...}
      3. Prose before/after: "Sure, here you go: {...}"
      4. Trailing commas: {"a": 1,}

    Strategy — fence strip → FAST PATH (raw_decode on stripped content) →
    SLOW PATH (strip reasoning XML, retry). The fast path runs FIRST so
    that legitimate JSON containing XML-shaped string values (e.g.
    {"comment": "<analysis>..."}) is never corrupted.

    NEVER raises. On total failure returns the (best-effort stripped)
    content so the caller's json.loads gives a clear error including
    the raw input.
    """
    if not content:
        return ""
    s = content.strip()

    fence_match = _FENCE_RE.match(s)
    if fence_match:
        s = fence_match.group(1).strip()

    result = _try_extract(s)
    if result is not None:
        return result

    stripped = _REASONING_XML_RE.sub("", s).strip()
    if stripped != s:
        result = _try_extract(stripped)
        if result is not None:
            return result
        s = stripped

    return s


def _parse_openai_tool_calls(choice) -> list[ToolCallDict]:
    """Extract tool calls from an OpenAI-style chat completion choice."""
    tool_calls: list[ToolCallDict] = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            try:
                parsed_args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                log.warning("Malformed tool_call arguments for %s",
                            tc.function.name)
                parsed_args = {}
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "arguments": parsed_args,
            })
    return tool_calls


def _openai_response(choice, usage, model: str, tool_calls: list[ToolCallDict]) -> LLMResponse:
    """Build LLMResponse from OpenAI-style completion."""
    reasoning: int | None = None
    cached: int | None = None
    if usage:
        comp_details = getattr(usage, "completion_tokens_details", None)
        if comp_details is not None:
            r = getattr(comp_details, "reasoning_tokens", None)
            reasoning = int(r) if r is not None else None
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            c = getattr(prompt_details, "cached_tokens", None)
            cached = int(c) if c is not None else None
    return LLMResponse(
        content=choice.message.content or "",
        tool_calls=tool_calls,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        model=model,
        finish_reason=choice.finish_reason or "",
        reasoning_tokens=reasoning,
        cached_prompt_tokens=cached,
    )


class _OpenAICompatChat:
    """Mixin: auto-negotiate max_tokens vs max_completion_tokens and temperature.

    On first call, tries the modern parameter set. If the API returns 400
    for an unsupported parameter, it retries with the legacy variant and
    caches the capability so subsequent calls don't need a retry.

    Learned capabilities are also persisted in a class-level cache keyed by
    model name, so a fresh provider instance for the same model (e.g. after
    a hot-swap) skips the probe-and-retry round-trip entirely.
    """

    # Class-level cache: model → {use_max_completion_tokens, use_temperature}
    # Survives provider instance recreation (hot-swap, pool reload).
    # GIL-safe: dict read/write is atomic; values are write-once per model.
    _model_caps: dict[str, dict[str, bool]] = {}

    # Class-level cache for response_format (json_mode) capability.
    # Keyed by (model, base_url) because the same model name on different
    # endpoints (e.g. real OpenAI vs self-hosted vLLM exposing "gpt-4o")
    # may have divergent json_mode support.
    # Value: True = supports response_format, False = does not.
    _json_mode_caps: dict[tuple[str, str], bool] = {}

    # Class-level cache for reasoning_effort capability.
    # Keyed by (model, base_url) — third-party gateways often pass through
    # reasoning models with different upstream support than direct API.
    # Value: True = supports reasoning_effort, False = does not.
    _reasoning_caps: dict[tuple[str, str], bool] = {}

    # Default reasoning_effort sent to reasoning-capable models. "minimal"
    # keeps chat-style replies fast; non-reasoning models reject it on first
    # call and we cache the negative for that (model, base_url).
    _REASONING_EFFORT_DEFAULT = "minimal"

    # Per-instance capability flags (set after first successful call)
    # None = unknown, True = supported, False = not supported
    _use_max_completion_tokens: bool | None = None
    _use_temperature: bool | None = None

    def _init_caps_from_cache(self, model: str) -> None:
        """Seed instance flags from class-level cache if available."""
        cached = self._model_caps.get(model)
        if cached:
            self._use_max_completion_tokens = cached.get("use_max_completion_tokens")
            self._use_temperature = cached.get("use_temperature")
            log.debug("Model %s: restored caps from cache "
                      "(max_completion_tokens=%s, temperature=%s)",
                      model, self._use_max_completion_tokens,
                      self._use_temperature)

    def _save_caps_to_cache(self, model: str) -> None:
        """Persist resolved capability flags to the class-level cache."""
        if self._use_max_completion_tokens is not None or self._use_temperature is not None:
            caps: dict[str, bool] = {}
            if self._use_max_completion_tokens is not None:
                caps["use_max_completion_tokens"] = self._use_max_completion_tokens
            if self._use_temperature is not None:
                caps["use_temperature"] = self._use_temperature
            self._model_caps[model] = caps

    def _do_chat(self, client, model: str, messages: list[dict],
                 tools: list[dict] | None, temperature: float,
                 max_tokens: int, json_mode: bool = False,
                 base_url: str = "") -> Any:
        """Call chat.completions.create with auto-negotiation."""
        from openai import BadRequestError

        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        # --- max tokens parameter ---
        if self._use_max_completion_tokens is None:
            # Unknown — try new param first
            kwargs["max_completion_tokens"] = max_tokens
        elif self._use_max_completion_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

        # --- temperature ---
        if self._use_temperature is None:
            # Unknown — include it (most models support it)
            kwargs["temperature"] = temperature
        elif self._use_temperature:
            kwargs["temperature"] = temperature
        # else: omit temperature entirely

        # --- json_mode (response_format) ---
        # Cache key uses base_url because the same model name on different
        # endpoints can have divergent capability.
        json_cache_key = (model, base_url)
        json_mode_supported = self._json_mode_caps.get(json_cache_key)
        sent_response_format = False
        if json_mode and json_mode_supported is not False:
            kwargs["response_format"] = {"type": "json_object"}
            sent_response_format = True

        # --- reasoning_effort ---
        # Send "minimal" by default to keep reasoning models (Gemini 3 Pro,
        # GPT-5, o-series) fast on chat workloads. Non-reasoning models will
        # reject it; the fallback below caches the negative per (model, base_url).
        reasoning_cache_key = (model, base_url)
        reasoning_supported = self._reasoning_caps.get(reasoning_cache_key)
        sent_reasoning = False
        if reasoning_supported is not False:
            kwargs["reasoning_effort"] = self._REASONING_EFFORT_DEFAULT
            sent_reasoning = True

        try:
            resp = client.chat.completions.create(**kwargs)
            # Success — lock in the capabilities
            if self._use_max_completion_tokens is None:
                self._use_max_completion_tokens = True
                log.debug("Model %s: using max_completion_tokens", model)
            if self._use_temperature is None:
                self._use_temperature = True
            if sent_response_format and json_mode_supported is None:
                self._json_mode_caps[json_cache_key] = True
                log.debug("Model %s @ %s: json_mode supported",
                          model, base_url or "default")
            if sent_reasoning and reasoning_supported is None:
                self._reasoning_caps[reasoning_cache_key] = True
                log.debug("Model %s @ %s: reasoning_effort supported",
                          model, base_url or "default")
            self._save_caps_to_cache(model)
            return resp
        except BadRequestError as e:
            err_msg = str(e).lower()
            retried = False

            # Handle max_tokens vs max_completion_tokens
            if "max_tokens" in err_msg and "max_completion_tokens" in err_msg:
                if self._use_max_completion_tokens is None:
                    # Was trying max_completion_tokens, need max_tokens
                    self._use_max_completion_tokens = False
                    kwargs.pop("max_completion_tokens", None)
                    kwargs["max_tokens"] = max_tokens
                    log.info("Model %s: falling back to max_tokens", model)
                    retried = True
                elif not self._use_max_completion_tokens:
                    # Was trying max_tokens, need max_completion_tokens
                    self._use_max_completion_tokens = True
                    kwargs.pop("max_tokens", None)
                    kwargs["max_completion_tokens"] = max_tokens
                    log.info("Model %s: falling back to max_completion_tokens", model)
                    retried = True

            # Handle unsupported temperature
            if "temperature" in err_msg and ("unsupported" in err_msg or "not supported" in err_msg):
                self._use_temperature = False
                kwargs.pop("temperature", None)
                log.info("Model %s: disabling temperature", model)
                retried = True

            # Handle unsupported response_format — broad fallback.
            # Don't match on error text; if we sent response_format and got
            # any 400, drop it and retry once. If retry also fails, the
            # original problem wasn't response_format.
            if sent_response_format:
                self._json_mode_caps[json_cache_key] = False
                kwargs.pop("response_format", None)
                sent_response_format = False
                log.info("Model %s @ %s: json_mode unsupported, falling back",
                         model, base_url or "default")
                retried = True

            # Handle unsupported reasoning_effort — same broad pattern as
            # response_format. If retry succeeds, original cause WAS one of
            # the dropped suspects (we cache reasoning=False). If retry also
            # fails, the cache write is still correct: this gateway/model
            # combo doesn't support it.
            if sent_reasoning:
                self._reasoning_caps[reasoning_cache_key] = False
                kwargs.pop("reasoning_effort", None)
                sent_reasoning = False
                log.info("Model %s @ %s: reasoning_effort unsupported, falling back",
                         model, base_url or "default")
                retried = True

            if retried:
                resp = client.chat.completions.create(**kwargs)
                # Lock in capabilities from the successful retry
                if self._use_max_completion_tokens is None:
                    self._use_max_completion_tokens = "max_completion_tokens" in kwargs
                if self._use_temperature is None:
                    self._use_temperature = "temperature" in kwargs
                self._save_caps_to_cache(model)
                return resp
            raise


class OpenAIProvider(_OpenAICompatChat, LLMProvider):
    """OpenAI-compatible API provider (works with OpenAI, DeepSeek, Ollama, Groq, etc.)."""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        from openai import OpenAI
        self._model = model
        self._base_url = base_url
        self._use_max_completion_tokens = None
        self._use_temperature = None
        self._init_caps_from_cache(model)
        kwargs: dict = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": _HTTP_TIMEOUT,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def provider_name(self) -> str:
        return "openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 1.0, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        resp = self._do_chat(self._client, self._model, messages, tools,
                             temperature, max_tokens, json_mode=json_mode,
                             base_url=self._base_url)
        choice = resp.choices[0]
        response = _openai_response(choice, resp.usage, self._model,
                                    _parse_openai_tool_calls(choice))
        if json_mode and response.content:
            response.content = extract_json(response.content)
        return response


class AzureOpenAIProvider(_OpenAICompatChat, LLMProvider):
    """Azure OpenAI API provider."""

    def __init__(self, api_key: str, model: str, base_url: str = "",
                 api_version: str = ""):
        from openai import AzureOpenAI
        self._deployment = model
        self._base_url = base_url
        self._use_max_completion_tokens = None
        self._use_temperature = None
        self._init_caps_from_cache(model)
        self._client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=api_version or AZURE_API_VERSION,
            max_retries=0,
            timeout=_HTTP_TIMEOUT,
        )

    def provider_name(self) -> str:
        return "azure_openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 1.0, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        resp = self._do_chat(self._client, self._deployment, messages, tools,
                             temperature, max_tokens, json_mode=json_mode,
                             base_url=self._base_url)
        choice = resp.choices[0]
        response = _openai_response(choice, resp.usage, self._deployment,
                                    _parse_openai_tool_calls(choice))
        if json_mode and response.content:
            response.content = extract_json(response.content)
        return response


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, model: str):
        import anthropic
        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def provider_name(self) -> str:
        return "anthropic"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 1.0, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        # Anthropic has no native JSON mode. Caller must rely on prompting.
        # Framework-layer strip below is the safety net (gated on json_mode).
        # Separate system message from conversation
        system_msg = ""
        conversation = []
        for m in messages:
            if m["role"] == "system":
                system_msg += m["content"] + "\n"
            else:
                conversation.append(m)

        # Convert OpenAI-format tool messages to Anthropic format
        conversation = self._convert_messages(conversation)

        kwargs = dict(
            model=self._model,
            messages=conversation,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if system_msg:
            kwargs["system"] = system_msg.strip()
        if tools:
            # Convert OpenAI tool format to Anthropic format
            kwargs["tools"] = self._convert_tools(tools)

        resp = self._client.messages.create(**kwargs)

        content = ""
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })

        if json_mode and content:
            content = extract_json(content)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.input_tokens if resp.usage else 0,
            completion_tokens=resp.usage.output_tokens if resp.usage else 0,
            total_tokens=(resp.usage.input_tokens + resp.usage.output_tokens) if resp.usage else 0,
            model=self._model,
            finish_reason=resp.stop_reason or "",
        )

    @staticmethod
    def _convert_tools(openai_tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Anthropic tool format."""
        anthropic_tools = []
        for t in openai_tools:
            func = t.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        return anthropic_tools

    @staticmethod
    def _convert_messages(messages: list[dict]) -> list[dict]:
        """Convert OpenAI-format tool messages to Anthropic format.

        OpenAI uses:
          - assistant msg with "tool_calls" list
          - separate "tool" role messages with tool_call_id
        Anthropic uses:
          - assistant msg with content blocks: [{"type":"tool_use","id":...,"name":...,"input":...}]
          - user msg with content blocks: [{"type":"tool_result","tool_use_id":...,"content":"..."}]
        """
        converted = []
        i = 0
        while i < len(messages):
            m = messages[i]

            if m["role"] == "assistant" and "tool_calls" in m:
                # Convert assistant tool_calls to content blocks
                content_blocks = []
                if m.get("content"):
                    content_blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    func = tc.get("function", {})
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": func.get("name", ""),
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": content_blocks})
                i += 1

            elif m["role"] == "tool":
                # Collect consecutive tool results into one user message
                result_blocks = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    result_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": messages[i].get("tool_call_id", ""),
                        "content": messages[i].get("content", ""),
                    })
                    i += 1
                converted.append({"role": "user", "content": result_blocks})

            else:
                converted.append(m)
                i += 1

        return converted


class GeminiProvider(LLMProvider):
    """Google Gemini API provider via the google-genai SDK."""

    def __init__(self, api_key: str, model: str):
        from google import genai
        model = self._normalize_model(model)
        self._model = model if model.startswith("models/") else f"models/{model}"
        self._client = genai.Client(api_key=api_key)

    @staticmethod
    def _normalize_model(model: str) -> str:
        """Normalize user-entered model name to Gemini API format.

        'Gemini 2.5 Flash' → 'gemini-2.5-flash'
        'gemini-2.5-flash' → 'gemini-2.5-flash' (no-op)
        'models/gemini-2.5-flash' → 'models/gemini-2.5-flash' (no-op)
        """
        m = model.strip().lower()
        # Collapse whitespace / underscores to hyphens
        import re
        m = re.sub(r"[\s_]+", "-", m)
        return m

    def provider_name(self) -> str:
        return "gemini"

    def _build_thinking_config(self, types):
        """Return a ThinkingConfig forcing low reasoning, or None if unsupported.

        Symmetric to OpenAI's reasoning_effort="minimal" default — keeps
        chat-style replies fast and avoids the Gemini 3 default of "high"
        burning tokens on every turn.

        Model family detection by name (after _normalize_model):
          'models/gemini-3*'   → ThinkingConfig(thinking_level="low")
          'models/gemini-2.5*' → ThinkingConfig(thinking_budget=512)
          everything else      → None (don't send the field at all)
        """
        m = self._model.lower()
        # _model has 'models/' prefix from __init__
        bare = m.removeprefix("models/")
        try:
            if bare.startswith("gemini-3"):
                return types.ThinkingConfig(thinking_level="low")
            if bare.startswith("gemini-2.5"):
                # 512 tokens — cheap floor that still allows minimal reflection.
                # Pro requires min 128; Flash accepts 0+. 512 is a safe shared default.
                return types.ThinkingConfig(thinking_budget=512)
        except (TypeError, AttributeError) as e:
            # SDK older than expected — silently skip.
            log.debug("Gemini ThinkingConfig unsupported on this SDK: %s", e)
        return None

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 1.0, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        from google.genai import types

        system_msg, contents = self._convert_messages(messages)

        config_kwargs: dict = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_msg:
            config_kwargs["system_instruction"] = system_msg.strip()
        if tools:
            config_kwargs["tools"] = [
                types.Tool(function_declarations=self._convert_tools(tools))
            ]
        if json_mode:
            # response_mime_type is stable across google-genai SDK versions.
            # Tool calls and JSON mode are mutually exclusive in Gemini, so
            # only set it when no tools requested.
            if not tools:
                config_kwargs["response_mime_type"] = "application/json"

        # Thinking budget — Gemini 3 defaults to thinking_level="high" if
        # omitted, which burns tokens on chat-style replies. Force "low"
        # symmetric to OpenAI's reasoning_effort="minimal" default.
        # Field name differs by model family:
        #   gemini-3*  → thinking_level="low"
        #   gemini-2.5* → thinking_budget=512 (token budget; min 128 for Pro)
        #   older       → no thinking config (field unsupported)
        thinking_cfg = self._build_thinking_config(types)
        if thinking_cfg is not None:
            config_kwargs["thinking_config"] = thinking_cfg

        resp = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        content = ""
        tool_calls: list[ToolCallDict] = []
        candidate = resp.candidates[0] if resp.candidates else None
        if candidate and candidate.content:
            for part in candidate.content.parts:
                if part.text:
                    content += part.text
                elif part.function_call:
                    fc = part.function_call
                    tool_calls.append({
                        "id": getattr(fc, "id", "") or fc.name,
                        "name": fc.name,
                        "arguments": dict(fc.args) if fc.args else {},
                    })

        if json_mode and content:
            content = extract_json(content)

        usage = resp.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0 if usage else 0
        completion_tokens = getattr(usage, "candidates_token_count", 0) or 0 if usage else 0

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            model=self._model,
            finish_reason=candidate.finish_reason.name if candidate and candidate.finish_reason else "",
        )

    @staticmethod
    def _convert_tools(openai_tools: list[dict]) -> list[dict]:
        """Convert OpenAI tool format to Gemini FunctionDeclaration dicts."""
        declarations = []
        for t in openai_tools:
            func = t.get("function", {})
            declarations.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {}),
            })
        return declarations

    @staticmethod
    def _convert_messages(messages: list[dict]) -> tuple[str, list]:
        """Convert OpenAI-format messages to Gemini contents.

        Returns (system_instruction, contents_list).
        Gemini uses 'user' and 'model' roles (not 'assistant').
        Tool results are sent as Part.from_function_response in a 'user' turn.
        """
        from google.genai import types

        system_msg = ""
        contents = []
        call_id_to_name = {}  # tool_call_id → function name mapping
        i = 0
        while i < len(messages):
            m = messages[i]

            if m["role"] == "system":
                system_msg += m["content"] + "\n"
                i += 1

            elif m["role"] == "user":
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=m["content"])])
                )
                i += 1

            elif m["role"] == "assistant":
                parts = []
                if m.get("content"):
                    parts.append(types.Part(text=m["content"]))
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        func = tc.get("function", {})
                        args = func.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except json.JSONDecodeError:
                                args = {}
                        fn_name = func.get("name", "")
                        # Map tool_call_id → name for later tool result lookup
                        tc_id = tc.get("id", "")
                        if tc_id and fn_name:
                            call_id_to_name[tc_id] = fn_name
                        parts.append(types.Part.from_function_call(
                            name=fn_name,
                            args=args,
                        ))
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                i += 1

            elif m["role"] == "tool":
                # Collect consecutive tool results into one user turn
                parts = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    tm = messages[i]
                    # Parse content as JSON if possible for structured response
                    tool_content = tm.get("content", "")
                    try:
                        result_data = json.loads(tool_content)
                    except (json.JSONDecodeError, TypeError):
                        result_data = {"result": tool_content}
                    parts.append(types.Part.from_function_response(
                        name=tm.get("name") or call_id_to_name.get(tm.get("tool_call_id", ""), "unknown"),
                        response=result_data,
                    ))
                    i += 1
                contents.append(types.Content(role="user", parts=parts))

            else:
                # Unknown role — treat as user
                contents.append(
                    types.Content(role="user", parts=[types.Part(text=m.get("content", ""))])
                )
                i += 1

        # Merge consecutive same-role turns.
        # Gemini requires strict user/model alternation. Proactive messages
        # (heartbeat, reminders) can produce consecutive assistant entries in
        # the conversation history, which become consecutive model turns here.
        # TODO: upstream cause is heartbeat/reminder saving assistant messages
        # without a preceding user message — this merge is the adapter-layer fix.
        if contents:
            merged: list = [contents[0]]
            for c in contents[1:]:
                if c.role == merged[-1].role:
                    merged[-1] = types.Content(
                        role=c.role,
                        parts=list(merged[-1].parts) + list(c.parts),
                    )
                else:
                    merged.append(c)
            contents = merged

        return system_msg, contents


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_config(purpose: str) -> tuple[str, str, str, str]:
    """Resolve (provider, api_key, model, base_url) for a given purpose.

    Think config falls back to Chat config field-by-field.
    """
    if purpose == "think":
        provider = THINK_PROVIDER or CHAT_PROVIDER
        api_key = THINK_API_KEY or CHAT_API_KEY
        model = THINK_MODEL or CHAT_MODEL
        base_url = THINK_BASE_URL or CHAT_BASE_URL
    else:
        provider = CHAT_PROVIDER
        api_key = CHAT_API_KEY
        model = CHAT_MODEL
        base_url = CHAT_BASE_URL
    return provider, api_key, model, base_url


def _make_client(provider: str, api_key: str, model: str, base_url: str) -> LLMProvider:
    """Instantiate a fresh LLM provider."""
    model = model.strip()
    if not model:
        raise ValueError(
            f"CHAT_MODEL (or THINK_MODEL) is required but not set. "
            "Please set it in your .env file."
        )
    if provider == "openai":
        return OpenAIProvider(api_key=api_key, model=model, base_url=base_url)
    elif provider == "azure_openai":
        return AzureOpenAIProvider(api_key=api_key, model=model, base_url=base_url)
    elif provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    elif provider == "gemini":
        return GeminiProvider(api_key=api_key, model=model)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Supported: openai (+ any compatible API), azure_openai, anthropic, gemini"
        )


def get_client_for_tier(tier: str = "chat") -> LLMProvider:
    """Get an LLM client via the model pool tier routing.

    Always delegates to ModelPool.get_tier(), which resolves:
    DB tier assignments > env TIER_* config > env CHAT_* fallback.
    """
    from mochi.model_pool import get_pool
    return get_pool().get_tier(tier)
