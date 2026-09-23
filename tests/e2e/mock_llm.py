"""Mock LLM provider for E2E tests.

Returns scripted LLMResponse objects in order, allowing tests to control
the full chat → tool-call → response cycle without any real API calls.
"""

import uuid
from copy import deepcopy

from mochi.llm import LLMProvider, LLMResponse, ToolCallDict


class MockLLMProvider(LLMProvider):
    """LLM provider that returns pre-scripted responses."""

    def __init__(self, responses: list[LLMResponse | Exception] | None = None):
        self._responses: list[LLMResponse | Exception] = list(responses or [])
        self.call_log: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.call_log.append({
            "messages": deepcopy(messages),
            "tools": deepcopy(tools),
            "max_tokens": max_tokens,
        })
        if not self._responses:
            return LLMResponse(content="(no more scripted responses)", model="mock")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def provider_name(self) -> str:
        return "mock"


def make_tool_call(name: str, arguments: dict, call_id: str | None = None) -> ToolCallDict:
    """Create a ToolCallDict for scripting LLM tool-call responses."""
    return {
        "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
        "name": name,
        "arguments": arguments,
        "argument_error": None,
    }


def make_response(
    content: str = "",
    tool_calls: list[ToolCallDict] | None = None,
) -> LLMResponse:
    """Create an LLMResponse for scripting."""
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        model="mock-model",
        finish_reason="tool_calls" if tool_calls else "stop",
        tool_calls_complete=bool(tool_calls),
    )
