from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from mochi import llm


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
        }],
    )

    assert response.reasoning_content == "I should check the weather first."


@pytest.mark.parametrize(
    "error_order",
    [
        ("tokens", "reasoning"),
        ("reasoning", "tokens"),
    ],
)
def test_openai_negotiates_reasoning_and_token_quirks(error_order):
    class FakeCompletions:
        def __init__(self):
            self.calls = []
            self.errors = list(error_order)

        def create(self, **kwargs):
            self.calls.append(kwargs)
            error = self.errors.pop(0) if self.errors else None
            if error:
                response = httpx.Response(
                    400,
                    request=httpx.Request(
                        "POST", "https://api.deepseek.com/v1/chat/completions",
                    ),
                )
                message = (
                    "Use `max_tokens` instead of `max_completion_tokens`."
                    if error == "tokens"
                    else "The `reasoning_content` in the thinking mode must be "
                    "passed back to the API."
                )
                raise BadRequestError(
                    message,
                    response=response,
                    body={"error": {"code": "invalid_request_error"}},
                )
            return object()

    provider = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    provider._use_max_completion_tokens = None
    provider._requires_reasoning_placeholders = False
    provider._model_caps = {}
    provider._init_caps_from_cache(
        "deepseek-reasoner", "https://api.deepseek.com/v1",
    )
    completions = FakeCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
    )
    messages = [
        {"role": "system", "content": "You are a companion."},
        {"role": "user", "content": "Earlier message"},
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "system", "content": "You now have some free time."},
    ]

    provider._do_chat(
        client, "deepseek-reasoner", messages, tools=[{"type": "function"}],
        temperature=None, max_tokens=64,
    )

    assert len(completions.calls) == 3
    assert "reasoning_content" not in messages[2]
    assert completions.calls[2]["messages"][2]["reasoning_content"] == ""
    assert "max_tokens" in completions.calls[2]

    provider._do_chat(
        client, "deepseek-reasoner", messages, tools=[{"type": "function"}],
        temperature=None, max_tokens=64,
    )
    assert len(completions.calls) == 4
    assert completions.calls[3]["messages"][2]["reasoning_content"] == ""
    assert "max_tokens" in completions.calls[3]


def test_openai_capabilities_are_isolated_by_endpoint():
    shared_cache = {
        "https://api.deepseek.com/v1::shared-model": {
            "requires_reasoning_placeholders": True,
        },
    }
    provider = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    provider._model_caps = shared_cache
    provider._use_max_completion_tokens = None
    provider._requires_reasoning_placeholders = False

    provider._init_caps_from_cache(
        "shared-model", "https://api.openai.com/v1",
    )

    assert provider._requires_reasoning_placeholders is False


def test_only_official_chat_providers_are_accepted(monkeypatch):
    monkeypatch.setattr(llm, "OpenAIProvider", lambda **kwargs: ("openai", kwargs))
    monkeypatch.setattr(
        llm, "AnthropicProvider", lambda **kwargs: ("anthropic", kwargs),
    )

    assert llm._make_client(
        "openai", "key", "deepseek-chat", "https://api.deepseek.com/v1",
    )[0] == "openai"
    assert llm._make_client(
        "openai",
        "key",
        "gemini-model",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )[0] == "openai"
    assert llm._make_client("anthropic", "key", "claude", "")[0] == "anthropic"
    for provider in ("azure_openai", "gemini", "deepseek", "custom"):
        with pytest.raises(ValueError):
            llm._make_client(provider, "key", "model", "")
    import mochi.admin.admin_db as admin_db

    monkeypatch.setattr(admin_db, "encrypt_api_key", lambda value: value)
    admin_db.upsert_model(
        "deepseek", "openai", "model", "key", "https://api.deepseek.com/v1",
    )
    admin_db.upsert_model(
        "apiyi", "openai", "claude-model", "key", "https://api.apiyi.com/v1",
    )
    admin_db.set_tier_assignment("main", "apiyi")
    from mochi.db import init_db
    init_db()
    assert admin_db.list_tier_assignments()["main"] == "apiyi"
    for unsafe in (
        "http://api.example.com/v1",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?token=value",
        "https://api.example.com/v1#fragment",
        "https://api.example.com:bad/v1",
        "https://api.exam\nple.com/v1",
        "https://api.example.com/v1/chat/completions",
    ):
        with pytest.raises(ValueError, match="HTTPS API root"):
            admin_db.upsert_model(
                "unsafe", "openai", "model", "key", unsafe,
            )
    with pytest.raises(ValueError, match="official API"):
        admin_db.upsert_model(
            "anthropic-proxy", "anthropic", "model", "key",
            "https://api.example.com/v1",
        )
