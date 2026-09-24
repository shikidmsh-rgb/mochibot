from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from mochi import llm


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


def test_admin_accepts_https_compatible_endpoint_and_rejects_unsafe_urls(monkeypatch):
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


def test_malformed_provider_tool_arguments_are_not_parsed():
    malformed_call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(name="weather", arguments='{"city":'),
    )

    class Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content="", reasoning_content="", tool_calls=[malformed_call],
                    ),
                    finish_reason="tool_calls",
                )],
                usage=None,
            )

    provider = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    provider._model = "model"
    provider._base_url = "https://api.deepseek.com/v1"
    provider._use_max_completion_tokens = None
    provider._requires_reasoning_placeholders = False
    provider._model_caps = {}
    provider._init_caps_from_cache(provider._model, provider._base_url)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )

    malformed = provider.chat(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert malformed.tool_calls[0]["arguments"] is None
    assert malformed.tool_calls[0]["argument_error"] == (
        "arguments were not valid JSON"
    )


def test_reasoning_placeholders_are_negotiated_per_endpoint_and_model():
    def _bad_request(message):
        return BadRequestError(
            message,
            response=httpx.Response(
                400,
                request=httpx.Request(
                    "POST", "https://api.deepseek.com/v1/chat/completions",
                ),
            ),
            body={"error": {"code": "invalid_request_error"}},
        )

    class NegotiatingCompletions:
        def __init__(self, errors):
            self.errors = list(errors)
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            error = self.errors.pop(0) if self.errors else ""
            if error == "reasoning":
                raise _bad_request(
                    "The reasoning_content in the thinking mode must be "
                    "passed back to the API."
                )
            if error == "tokens":
                raise _bad_request(
                    "Use max_tokens instead of max_completion_tokens."
                )
            return SimpleNamespace()

    history = [
        {"role": "user", "content": "Earlier message"},
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "user", "content": "Check the weather"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should check the weather first.",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"ok":true}'},
    ]
    shared_cache = {}
    negotiating = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    negotiating._model_caps = shared_cache
    negotiating._use_max_completion_tokens = True
    negotiating._requires_reasoning_placeholders = False
    negotiating._init_caps_from_cache(
        "deepseek-reasoner", "https://api.deepseek.com/v1",
    )
    completions = NegotiatingCompletions(["reasoning"])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    negotiating._do_chat(
        client,
        "deepseek-reasoner",
        history,
        tools=[{"type": "function"}],
        temperature=None,
        max_tokens=64,
    )

    assert "reasoning_content" not in completions.calls[0]["messages"][1]
    assert completions.calls[1]["messages"][1]["reasoning_content"] == ""
    assert completions.calls[1]["messages"][3]["reasoning_content"] == (
        "I should check the weather first."
    )
    assert "reasoning_content" not in history[1]
    assert history[3]["reasoning_content"] == "I should check the weather first."

    cached = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
    cached._model_caps = shared_cache
    cached._use_max_completion_tokens = None
    cached._requires_reasoning_placeholders = False
    cached._init_caps_from_cache(
        "deepseek-reasoner", "https://api.deepseek.com/v1",
    )
    cached_calls = NegotiatingCompletions([])
    cached._do_chat(
        SimpleNamespace(
            chat=SimpleNamespace(completions=cached_calls),
        ),
        "deepseek-reasoner",
        history,
        tools=[{"type": "function"}],
        temperature=None,
        max_tokens=64,
    )
    assert cached_calls.calls[0]["messages"][1]["reasoning_content"] == ""

    for endpoint, model in (
        ("https://api.openai.com/v1", "deepseek-reasoner"),
        ("https://api.deepseek.com/V1", "deepseek-reasoner"),
        ("https://api.deepseek.com/v1", "another-model"),
    ):
        isolated = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
        isolated._model_caps = shared_cache
        isolated._use_max_completion_tokens = None
        isolated._requires_reasoning_placeholders = False
        isolated._init_caps_from_cache(model, endpoint)
        assert isolated._requires_reasoning_placeholders is False

    for error_order in (
        ("tokens", "reasoning"),
        ("reasoning", "tokens"),
    ):
        ordered = llm.OpenAIProvider.__new__(llm.OpenAIProvider)
        ordered._model_caps = {}
        ordered._use_max_completion_tokens = None
        ordered._requires_reasoning_placeholders = False
        ordered._init_caps_from_cache(
            "deepseek-reasoner", "https://api.deepseek.com/v1",
        )
        ordered_calls = NegotiatingCompletions(error_order)
        ordered._do_chat(
            SimpleNamespace(
                chat=SimpleNamespace(completions=ordered_calls),
            ),
            "deepseek-reasoner",
            history,
            tools=[{"type": "function"}],
            temperature=None,
            max_tokens=64,
        )
        assert len(ordered_calls.calls) == 3
        assert "max_tokens" in ordered_calls.calls[-1]
        assert (
            ordered_calls.calls[-1]["messages"][1]["reasoning_content"] == ""
        )
