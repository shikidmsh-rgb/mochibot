"""LLM provider abstraction — provider-agnostic.

Supports the OpenAI-compatible protocol for OpenAI/DeepSeek and Anthropic.

Usage:
    from mochi.llm import get_client_for_tier
    client = get_client_for_tier()         # main tier (default)
    client = get_client_for_tier("lite")   # optional low-cost tier
    response = client.chat(messages, tools=...)
"""

import json
import base64
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypedDict

import httpx

log = logging.getLogger(__name__)

# Explicit timeout for OpenAI-compatible HTTP clients. SDK default is 600s read,
# which silently masks slow gateways. Read=120s is well above worst-case
# reasoning-model latency on slow third-party gateways but fails fast on hangs.
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


def _decode_data_image(block: dict) -> tuple[str, bytes]:
    """Decode one canonical OpenAI image_url block for native providers."""
    image_url = block.get("image_url", {})
    url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:image/"):
        raise ValueError("Only base64 data URL images are supported")
    header, encoded = url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Image data URL must use base64 encoding")
    media_type = header[5:].split(";", 1)[0]
    return media_type, base64.b64decode(encoded, validate=True)


class ToolCallDict(TypedDict):
    """Typed structure for a single tool call in LLMResponse."""
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    content: str = ""
    reasoning_content: str = ""
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
             temperature: float | None = None, max_tokens: int = 2048,
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
        reasoning_content=getattr(choice.message, "reasoning_content", "") or "",
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
    """Mixin: negotiate max_tokens vs max_completion_tokens.

    On first call, tries the modern parameter set. If the API returns 400
    explicitly naming the token parameters, it retries with the other variant and
    caches the capability so subsequent calls don't need a retry.

    Learned capabilities are also persisted in a class-level cache keyed by
    model name, so a fresh provider instance for the same model (e.g. after
    a hot-swap) skips the probe-and-retry round-trip entirely.
    """

    # Class-level cache: endpoint + model → negotiated OpenAI-compatible quirks.
    # Survives provider instance recreation (hot-swap, pool reload).
    # GIL-safe: dict read/write is atomic; values are write-once per model.
    _model_caps: dict[str, dict[str, bool]] = {}

    # Per-instance capability flags (set after first successful call)
    # None = unknown, True = supported, False = not supported
    _use_max_completion_tokens: bool | None = None
    _requires_reasoning_placeholders: bool = False

    def _init_caps_from_cache(self, model: str, base_url: str = "") -> None:
        """Seed instance flags from class-level cache if available."""
        endpoint = base_url.rstrip("/").lower() or "openai-default"
        self._caps_cache_key = f"{endpoint}::{model}"
        cached = self._model_caps.get(self._caps_cache_key)
        if cached:
            self._use_max_completion_tokens = cached.get("use_max_completion_tokens")
            self._requires_reasoning_placeholders = cached.get(
                "requires_reasoning_placeholders", False,
            )
            log.debug(
                "Model %s: restored max_completion_tokens=%s, "
                "reasoning_placeholders=%s from cache",
                model, self._use_max_completion_tokens,
                self._requires_reasoning_placeholders,
            )

    def _save_caps_to_cache(self, model: str) -> None:
        """Persist resolved capability flags to the class-level cache."""
        cache_key = getattr(self, "_caps_cache_key", model)
        cached = dict(self._model_caps.get(cache_key, {}))
        if self._use_max_completion_tokens is not None:
            cached["use_max_completion_tokens"] = self._use_max_completion_tokens
        if self._requires_reasoning_placeholders:
            cached["requires_reasoning_placeholders"] = True
        if cached:
            self._model_caps[cache_key] = cached

    @staticmethod
    def _with_reasoning_placeholders(messages: list[dict]) -> list[dict]:
        """Copy assistant history with explicit empty reasoning placeholders."""
        return [
            (
                {**message, "reasoning_content": ""}
                if message.get("role") == "assistant"
                and "reasoning_content" not in message
                else message
            )
            for message in messages
        ]

    def _do_chat(self, client, model: str, messages: list[dict],
                 tools: list[dict] | None, temperature: float | None,
                 max_tokens: int, json_mode: bool = False) -> Any:
        """Call chat.completions.create with auto-negotiation."""
        from openai import BadRequestError

        request_messages = (
            self._with_reasoning_placeholders(messages)
            if self._requires_reasoning_placeholders
            else messages
        )
        kwargs: dict = {"model": model, "messages": request_messages}
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

        if temperature is not None:
            kwargs["temperature"] = temperature

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        for attempt in range(3):
            try:
                resp = client.chat.completions.create(**kwargs)
                if self._use_max_completion_tokens is None:
                    self._use_max_completion_tokens = True
                    log.debug("Model %s: using max_completion_tokens", model)
                self._save_caps_to_cache(model)
                return resp
            except BadRequestError as exc:
                err_msg = str(exc).lower()
                changed = False

                if "max_tokens" in err_msg and "max_completion_tokens" in err_msg:
                    if self._use_max_completion_tokens is None:
                        self._use_max_completion_tokens = False
                        kwargs.pop("max_completion_tokens", None)
                        kwargs["max_tokens"] = max_tokens
                        log.info("Model %s: falling back to max_tokens", model)
                        changed = True
                    elif not self._use_max_completion_tokens:
                        self._use_max_completion_tokens = True
                        kwargs.pop("max_tokens", None)
                        kwargs["max_completion_tokens"] = max_tokens
                        log.info(
                            "Model %s: falling back to max_completion_tokens",
                            model,
                        )
                        changed = True

                if (
                    "reasoning_content" in err_msg
                    and "must be passed back" in err_msg
                    and not self._requires_reasoning_placeholders
                ):
                    normalized = self._with_reasoning_placeholders(messages)
                    if normalized != messages:
                        self._requires_reasoning_placeholders = True
                        kwargs["messages"] = normalized
                        log.info(
                            "Model %s: adding empty reasoning placeholders to "
                            "assistant history",
                            model,
                        )
                        changed = True

                if not changed or attempt == 2:
                    raise

        raise RuntimeError("OpenAI-compatible capability negotiation exhausted")


class OpenAIProvider(_OpenAICompatChat, LLMProvider):
    """OpenAI-compatible API provider for OpenAI and DeepSeek."""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        from openai import OpenAI
        self._model = model
        self._base_url = base_url
        self._use_max_completion_tokens = None
        self._requires_reasoning_placeholders = False
        self._init_caps_from_cache(model, base_url)
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
             temperature: float | None = None, max_tokens: int = 2048,
             json_mode: bool = False) -> LLMResponse:
        resp = self._do_chat(self._client, self._model, messages, tools,
                             temperature, max_tokens, json_mode=json_mode)
        choice = resp.choices[0]
        response = _openai_response(choice, resp.usage, self._model,
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
             temperature: float | None = None, max_tokens: int = 2048,
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
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        if system_msg:
            # System as a list-of-blocks with cache_control: ephemeral.
            # Mochi's system prompt (Core + Agent + runtime) is 4-8KB and
            # 100% stable across a conversation — perfect cache target.
            # Cached reads bill at 10% of input rate.
            kwargs["system"] = [{
                "type": "text",
                "text": system_msg.strip(),
                "cache_control": {"type": "ephemeral"},
            }]
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
            elif block.type in ("thinking", "redacted_thinking"):
                # Internal reasoning — NEVER leak into user-facing content.
                continue

        if json_mode and content:
            content = extract_json(content)

        usage = resp.usage
        cached: int | None = None
        if usage:
            cache_read = getattr(usage, "cache_read_input_tokens", None)
            cache_create = getattr(usage, "cache_creation_input_tokens", None)
            if cache_read is not None or cache_create is not None:
                # Only "read" counts as savings. cache_creation is the FIRST
                # write (full price + 25% surcharge) — don't conflate.
                cached = int(cache_read or 0)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=usage.input_tokens if usage else 0,
            completion_tokens=usage.output_tokens if usage else 0,
            total_tokens=(usage.input_tokens + usage.output_tokens) if usage else 0,
            model=self._model,
            finish_reason=resp.stop_reason or "",
            # Anthropic doesn't separately report thinking-token usage; it's
            # bundled into output_tokens. Leave None to preserve the P1-2
            # semantic (None = not reported by SDK).
            reasoning_tokens=None,
            cached_prompt_tokens=cached,
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
                if m["role"] == "user" and isinstance(m.get("content"), list):
                    blocks = []
                    for block in m["content"]:
                        if block.get("type") == "text":
                            blocks.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "image_url":
                            media_type, data = _decode_data_image(block)
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(data).decode("ascii"),
                                },
                            })
                    converted.append({**m, "content": blocks})
                else:
                    converted.append(m)
                i += 1

        return converted


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def _make_client(provider: str, api_key: str, model: str, base_url: str) -> LLMProvider:
    """Instantiate a fresh LLM provider."""
    model = model.strip()
    if not model:
        raise ValueError(
            "Model name is required. Configure it in the admin portal."
        )
    if provider == "openai":
        return OpenAIProvider(api_key=api_key, model=model, base_url=base_url)
    elif provider == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model)
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Supported: openai (including compatible APIs) and anthropic"
        )


def get_client_for_tier(tier: str = "main") -> LLMProvider:
    """Get an LLM client via the model pool tier routing.

    Always delegates to ModelPool.get_tier(), which resolves DB tier
    assignments.
    """
    from mochi.model_pool import get_pool
    return get_pool().get_tier(tier)
