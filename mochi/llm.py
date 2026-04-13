"""LLM provider abstraction — provider-agnostic.

Supports any OpenAI-compatible API, Azure OpenAI, and Anthropic.

Usage:
    from mochi.llm import get_client_for_tier
    client = get_client_for_tier()         # chat tier (default)
    client = get_client_for_tier("deep")   # deep tier
    response = client.chat(messages, tools=...)
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypedDict

from mochi.config import (
    CHAT_PROVIDER, CHAT_API_KEY, CHAT_MODEL, CHAT_BASE_URL,
    THINK_PROVIDER, THINK_API_KEY, THINK_MODEL, THINK_BASE_URL,
    AZURE_API_VERSION,
)

log = logging.getLogger(__name__)


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


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
        """Send a chat completion request."""
        ...

    @abstractmethod
    def provider_name(self) -> str:
        ...


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
    return LLMResponse(
        content=choice.message.content or "",
        tool_calls=tool_calls,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        total_tokens=usage.total_tokens if usage else 0,
        model=model,
        finish_reason=choice.finish_reason or "",
    )


class _OpenAICompatChat:
    """Mixin: auto-negotiate max_tokens vs max_completion_tokens and temperature.

    On first call, tries the modern parameter set. If the API returns 400
    for an unsupported parameter, it retries with the legacy variant and
    caches the capability so subsequent calls don't need a retry.
    """

    # Per-instance capability flags (set after first successful call)
    # None = unknown, True = supported, False = not supported
    _use_max_completion_tokens: bool | None = None
    _use_temperature: bool | None = None

    def _do_chat(self, client, model: str, messages: list[dict],
                 tools: list[dict] | None, temperature: float,
                 max_tokens: int) -> Any:
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

        try:
            resp = client.chat.completions.create(**kwargs)
            # Success — lock in the capabilities
            if self._use_max_completion_tokens is None:
                self._use_max_completion_tokens = True
                log.debug("Model %s: using max_completion_tokens", model)
            if self._use_temperature is None:
                self._use_temperature = True
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

            if retried:
                resp = client.chat.completions.create(**kwargs)
                # Lock in capabilities from the successful retry
                if self._use_max_completion_tokens is None:
                    self._use_max_completion_tokens = "max_completion_tokens" in kwargs
                if self._use_temperature is None:
                    self._use_temperature = "temperature" in kwargs
                return resp
            raise


class OpenAIProvider(_OpenAICompatChat, LLMProvider):
    """OpenAI-compatible API provider (works with OpenAI, DeepSeek, Ollama, Groq, etc.)."""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        from openai import OpenAI
        self._model = model
        self._use_max_completion_tokens = None
        self._use_temperature = None
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def provider_name(self) -> str:
        return "openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
        resp = self._do_chat(self._client, self._model, messages, tools,
                             temperature, max_tokens)
        choice = resp.choices[0]
        return _openai_response(choice, resp.usage, self._model,
                                _parse_openai_tool_calls(choice))


class AzureOpenAIProvider(_OpenAICompatChat, LLMProvider):
    """Azure OpenAI API provider."""

    def __init__(self, api_key: str, model: str, base_url: str = "",
                 api_version: str = ""):
        from openai import AzureOpenAI
        self._deployment = model
        self._use_max_completion_tokens = None
        self._use_temperature = None
        self._client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=api_version or AZURE_API_VERSION,
        )

    def provider_name(self) -> str:
        return "azure_openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
        resp = self._do_chat(self._client, self._deployment, messages, tools,
                             temperature, max_tokens)
        choice = resp.choices[0]
        return _openai_response(choice, resp.usage, self._deployment,
                                _parse_openai_tool_calls(choice))


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str, model: str):
        import anthropic
        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def provider_name(self) -> str:
        return "anthropic"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
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
    else:
        raise ValueError(
            f"Unknown provider: {provider!r}. "
            "Supported: openai (+ any compatible API), azure_openai, anthropic"
        )


def get_client_for_tier(tier: str = "chat") -> LLMProvider:
    """Get an LLM client via the model pool tier routing.

    Always delegates to ModelPool.get_tier(), which resolves:
    DB tier assignments > env TIER_* config > env CHAT_* fallback.
    """
    from mochi.model_pool import get_pool
    return get_pool().get_tier(tier)
