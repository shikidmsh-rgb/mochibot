"""LLM provider abstraction — provider-agnostic.

Supports any OpenAI-compatible API, Azure OpenAI, and Anthropic.
Chat and Think can use completely independent providers/models/endpoints.

Usage:
    from mochi.llm import get_client
    client = get_client()            # chat model
    client = get_client("think")     # think model (falls back to chat)
    response = client.chat(messages, tools=...)
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from mochi.config import (
    CHAT_PROVIDER, CHAT_API_KEY, CHAT_MODEL, CHAT_BASE_URL,
    THINK_PROVIDER, THINK_API_KEY, THINK_MODEL, THINK_BASE_URL,
    AZURE_API_VERSION,
)

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    """Unified response from any LLM provider."""
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
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


def _is_o_series(model: str) -> bool:
    """Detect o-series / reasoning models that don't support temperature
    and require max_completion_tokens instead of max_tokens.
    Matches: o1, o1-mini, o3, o3-mini, o4-mini, gpt-o*, *5.1*, etc.
    """
    import re
    return bool(re.search(r'(^o\d|[/-]o\d|5\.1|o-series)', model, re.IGNORECASE))


def _build_completion_kwargs(
    model: str, messages: list[dict], temperature: float, max_tokens: int
) -> dict:
    """Build kwargs for chat.completions.create, adapting to model capabilities."""
    kwargs: dict = {"model": model, "messages": messages}
    if _is_o_series(model):
        # o-series: no temperature, use max_completion_tokens
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["temperature"] = temperature
        kwargs["max_tokens"] = max_tokens
    return kwargs


class OpenAIProvider(LLMProvider):
    """OpenAI-compatible API provider (works with OpenAI, DeepSeek, Ollama, Groq, etc.)."""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        from openai import OpenAI
        self._model = model
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    def provider_name(self) -> str:
        return "openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
        kwargs = _build_completion_kwargs(self._model, messages, temperature, max_tokens)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
            model=self._model,
            finish_reason=choice.finish_reason or "",
        )


class AzureOpenAIProvider(LLMProvider):
    """Azure OpenAI API provider."""

    def __init__(self, api_key: str, model: str, base_url: str = "",
                 api_version: str = ""):
        from openai import AzureOpenAI
        self._deployment = model
        self._client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=api_version or AZURE_API_VERSION,
        )

    def provider_name(self) -> str:
        return "azure_openai"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.7, max_tokens: int = 2048) -> LLMResponse:
        kwargs = _build_completion_kwargs(self._deployment, messages, temperature, max_tokens)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]

        tool_calls = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

        return LLMResponse(
            content=choice.message.content or "",
            tool_calls=tool_calls,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
            model=self._deployment,
            finish_reason=choice.finish_reason or "",
        )


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

_cached_clients: dict[str, LLMProvider] = {}  # keyed by purpose


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


def get_client(purpose: str = "chat") -> LLMProvider:
    """Get (or create) the LLM client for a given purpose.

    purpose:
        "chat"  — main conversation (default)
        "think" — heartbeat Think + maintenance (falls back to Chat config)
    """
    if purpose in _cached_clients:
        return _cached_clients[purpose]

    provider, api_key, model, base_url = _resolve_config(purpose)
    client = _make_client(provider, api_key, model, base_url)

    if purpose == "think" and (THINK_MODEL or THINK_PROVIDER):
        log.info("LLM [%s]: provider=%s model=%s", purpose, provider, model)
    else:
        log.info("LLM [%s]: provider=%s model=%s", purpose, provider, model)

    _cached_clients[purpose] = client
    return client
