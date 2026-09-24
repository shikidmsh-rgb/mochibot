import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI

from mochi.llm import OpenAIProvider, _responses_input


class OutputItem(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {
            key: value for key, value in vars(self).items()
            if value is not None
        }


def _item(type, **kwargs):
    return OutputItem(type=type, **kwargs)


def _response(*output, status="completed"):
    return SimpleNamespace(
        status=status,
        output=list(output),
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            input_tokens_details=SimpleNamespace(cached_tokens=5),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        ),
        incomplete_details="max_output_tokens",
    )


def _provider(responses):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider._model = "gpt-6-sol"
    provider._init_caps_from_cache("gpt-6-sol", "https://example.openai.azure.com/openai/v1")
    provider._client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return provider, calls


def test_gpt6_responses_tool_round_preserves_reasoning_and_function_pairing():
    reasoning = _item(
        "reasoning", id="rs_1", summary=[], encrypted_content="opaque",
    )
    call = _item(
        "function_call", id="fc_1", call_id="call_1", name="weather",
        arguments='{"city":"Tokyo"}', status="completed",
    )
    final = _item(
        "message", role="assistant", id="msg_1", phase="final_answer",
        status="completed", content=[_item("output_text", text="Sunny")],
    )
    provider, calls = _provider([
        _response(reasoning, call),
        _response(final),
    ])
    tool = {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Read weather",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    messages = [{"role": "system", "content": "You are Mochi"},
                {"role": "user", "content": "Weather?"}]
    first = provider.chat(messages, tools=[tool])
    assert calls[0]["store"] is False
    assert calls[0]["include"] == ["reasoning.encrypted_content"]
    assert calls[0]["tools"] == [{
        "type": "function", **tool["function"], "strict": False,
    }]
    assert calls[0]["input"] == messages
    assert first.tool_calls == [{
        "id": "call_1", "name": "weather",
        "arguments": {"city": "Tokyo"}, "argument_error": None,
    }]
    assert first.tool_calls_complete is True
    assert first.reasoning_content == json.dumps([reasoning.model_dump()])
    assert first.reasoning_tokens == 3
    assert first.cached_prompt_tokens == 5

    messages += [
        {"role": "assistant", "content": first.content,
         "reasoning_content": first.reasoning_content,
         "response_items": first.response_items},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok":true}'},
    ]
    second = provider.chat(messages, tools=[tool])
    assert calls[1]["input"][-3:] == [
        reasoning.model_dump(),
        call.model_dump(),
        {"type": "function_call_output", "call_id": "call_1",
         "output": '{"ok":true}'},
    ]
    assert second.content == "Sunny"
    assert second.tool_calls == []
    assert second.prompt_tokens == 20
    assert second.completion_tokens == 10
    assert second.total_tokens == 30


def test_gpt6_replays_delivered_encrypted_reasoning_and_converts_image():
    history = [
        {"role": "assistant", "content": "Earlier reply",
         "reasoning_content": '[{"type":"reasoning","encrypted_content":"opaque"}]'},
        {"role": "user", "content": [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,AAAA", "detail": "auto",
            }},
        ]},
    ]
    assert _responses_input(history) == [
        {"type": "reasoning", "encrypted_content": "opaque"},
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "What is this?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA",
             "detail": "auto"},
        ]},
    ]


@pytest.mark.parametrize("arguments,status,expected_error", [
    ('{"city":', "completed", "arguments were not valid JSON"),
    ('{"city":"Tokyo"}', "incomplete", None),
])
def test_gpt6_does_not_execute_bad_or_incomplete_function_calls(
    arguments, status, expected_error,
):
    provider, _ = _provider([_response(_item(
        "function_call", call_id="call_2", name="weather",
        arguments=arguments, status=status,
    ), status=status)])
    result = provider.chat([{"role": "user", "content": "Weather?"}])
    assert result.tool_calls[0]["argument_error"] == expected_error
    assert result.tool_calls_complete is (status == "completed")


def test_gpt6_rejects_incomplete_text_and_other_failure_statuses():
    provider, _ = _provider([_response(status="incomplete"),
                             _response(status="failed")])
    with pytest.raises(ValueError, match="incomplete output"):
        provider.chat([{"role": "user", "content": "Hi"}])
    with pytest.raises(ValueError, match="status 'failed'"):
        provider.chat([{"role": "user", "content": "Hi"}])


def test_other_models_keep_chat_completions():
    provider, _ = _provider([])
    provider._model = "gpt-5.6-sol"
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="Hello", tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        ))),
    )
    assert provider.chat([{"role": "user", "content": "Hi"}]).content == "Hello"


def test_openai_sdk_serializes_stateless_tool_round_with_reasoning():
    requests = []

    def handle(request):
        assert request.url.path.endswith("/responses")
        requests.append(json.loads(request.content))
        output = (
            [
                {"type": "reasoning", "id": "rs_1", "summary": [],
                 "encrypted_content": "opaque", "status": "completed"},
                {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                 "name": "weather", "arguments": "{}", "status": "completed"},
            ]
            if len(requests) == 1 else
            [{"type": "message", "id": "msg_1", "role": "assistant",
              "status": "completed", "content": [
                  {"type": "output_text", "text": "Sunny", "annotations": []},
              ]}]
        )
        return httpx.Response(200, json={
            "id": f"resp_{len(requests)}", "object": "response",
            "created_at": 1, "model": "gpt-6-sol", "status": "completed",
            "output": output, "usage": {
                "input_tokens": 2, "output_tokens": 3, "total_tokens": 5,
            },
        })

    provider, _ = _provider([])
    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        provider._client = OpenAI(
            api_key="test-only", base_url="https://example.openai.azure.com/openai/v1",
            http_client=http_client, max_retries=0,
        )
        messages = [{"role": "user", "content": "Weather?"}]
        first = provider.chat(messages, tools=[{
            "type": "function", "function": {
                "name": "weather", "parameters": {"type": "object"},
            },
        }])
        messages += [
            {"role": "assistant", "content": first.content,
             "response_items": first.response_items},
            {"role": "tool", "tool_call_id": first.tool_calls[0]["id"],
             "content": "ok"},
        ]
        assert provider.chat(messages).content == "Sunny"

    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["store"] is False
    assert requests[1]["input"][-3:] == [
        first.response_items[0],
        first.response_items[1],
        {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
    ]
