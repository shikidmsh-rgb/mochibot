"""Provider protocol translation contract."""

from types import SimpleNamespace

from mochi import llm
from mochi.tool_availability import ToolAvailability


def test_openai_response_preserves_reasoning_content_for_tool_followup():
    message = SimpleNamespace(
        content="",
        reasoning_content="I should check the weather first.",
    )
    choice = SimpleNamespace(message=message, finish_reason="tool_calls")

    response = llm._openai_response(
        choice,
        usage=None,
        model="deepseek-reasoner",
        tool_calls=[{
            "id": "call_weather",
            "name": "get_weather",
            "arguments": {"city": "Suzhou"},
            "argument_error": None,
        }],
    )

    assert response.reasoning_content == (
        "I should check the weather first."
    )
    assert response.tool_calls_complete is True

    usage = SimpleNamespace(
        prompt_tokens=453,
        completion_tokens=23,
        total_tokens=476,
        completion_tokens_details=None,
        prompt_tokens_details=None,
        prompt_cache_hit_tokens=384,
    )
    deepseek_usage = llm._openai_response(
        choice,
        usage=usage,
        model="deepseek-v4-flash",
        tool_calls=[],
    )
    assert deepseek_usage.cached_prompt_tokens == 384

    usage.prompt_tokens_details = SimpleNamespace(cached_tokens=256)
    standard_usage = llm._openai_response(
        choice,
        usage=usage,
        model="deepseek-v4-flash",
        tool_calls=[],
    )
    assert standard_usage.cached_prompt_tokens == 256

    valid_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="weather", arguments='{"city":"Tokyo"}'),
    )
    valid_choice = SimpleNamespace(
        message=SimpleNamespace(content="", tool_calls=[valid_call]),
        finish_reason="tool_calls",
    )
    assert llm._parse_openai_tool_calls(valid_choice) == [{
        "id": "call-1",
        "name": "weather",
        "arguments": {"city": "Tokyo"},
        "argument_error": None,
    }]

    malformed_call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(name="weather", arguments='{"city":'),
    )
    malformed_choice = SimpleNamespace(
        message=SimpleNamespace(content="", tool_calls=[malformed_call]),
        finish_reason="tool_calls",
    )
    malformed = llm._parse_openai_tool_calls(malformed_choice)
    assert malformed[0]["arguments"] is None
    assert malformed[0]["argument_error"] == "arguments were not valid JSON"

    incomplete = llm._openai_response(
        SimpleNamespace(message=message, finish_reason="length"),
        usage=None,
        model="deepseek-reasoner",
        tool_calls=malformed,
    )
    assert incomplete.tool_calls_complete is False

    anthropic_provider = llm.AnthropicProvider.__new__(llm.AnthropicProvider)
    anthropic_provider._model = "claude"
    anthropic_provider._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            content=[SimpleNamespace(
                type="tool_use",
                id="tool-2",
                name="weather",
                input={"city": "Tokyo"},
            )],
            usage=SimpleNamespace(input_tokens=4, output_tokens=3),
            stop_reason="tool_use",
        )),
    )
    anthropic_result = anthropic_provider.chat(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert anthropic_result.tool_calls == [{
        "id": "tool-2",
        "name": "weather",
        "arguments": {"city": "Tokyo"},
        "argument_error": None,
    }]
    assert anthropic_result.tool_calls_complete is True

    availability = ToolAvailability.from_definitions([{
        "type": "function",
        "function": {
            "name": "nullable",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": ["integer", "null"]}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }], source="test")
    assert availability.validate_arguments("nullable", {"value": None}) is None
    assert availability.validate_arguments(
        "nullable", {"value": "wrong"},
    ) == "arguments.value must be one of ['integer', 'null']"
    assert availability.validate_arguments("nullable", {}) == (
        "arguments.value is required"
    )
