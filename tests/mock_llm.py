"""Normalized model outputs for runtime boundaries, not a provider simulator."""

from copy import deepcopy
import uuid

from mochi.llm import LLMProvider, LLMResponse


class MockLLMProvider(LLMProvider):
    def __init__(self, responses):
        self._responses = list(responses)
        self.call_log = []

    def chat(self, messages, tools=None, temperature=None, max_tokens=2048):
        self.call_log.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        if not self._responses:
            raise AssertionError("Unexpected additional model call")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def provider_name(self):
        return "mock"


def make_tool_call(name, arguments, call_id=None):
    return {
        "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
        "name": name, "arguments": arguments, "argument_error": None,
    }


def make_response(content="", tool_calls=None):
    return LLMResponse(
        content=content, tool_calls=tool_calls or [], model="mock",
        finish_reason="tool_calls" if tool_calls else "stop",
        tool_calls_complete=bool(tool_calls),
    )
