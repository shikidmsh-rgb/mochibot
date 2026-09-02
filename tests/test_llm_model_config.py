import asyncio
import json
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


def test_openai_compatible_chat_handles_text_and_tools(monkeypatch):
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="weather", arguments='{"city":"Tokyo"}'),
    )
    malformed_call = SimpleNamespace(
        id="call-2",
        function=SimpleNamespace(name="weather", arguments='{"city":'),
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hello", tool_calls=[]),
                finish_reason="stop",
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="",
                    reasoning_content="I should check the weather first.",
                    tool_calls=[tool_call],
                ),
               finish_reason="tool_calls",
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
               message=SimpleNamespace(content="", tool_calls=[malformed_call]),
               finish_reason="tool_calls",
            )],
            usage=None,
        ),
    ]

    class Completions:
        def create(self, **kwargs):
            return responses.pop(0)

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

    assert provider.chat([{"role": "user", "content": "hi"}]).content == "hello"
    result = provider.chat(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert result.tool_calls == [{
        "id": "call-1",
        "name": "weather",
        "arguments": {"city": "Tokyo"},
        "argument_error": None,
    }]
    assert result.tool_calls_complete is True
    assert result.reasoning_content == "I should check the weather first."

    malformed = provider.chat(
        [{"role": "user", "content": "weather"}],
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert malformed.tool_calls[0]["arguments"] is None
    assert malformed.tool_calls[0]["argument_error"] == (
        "arguments were not valid JSON"
    )
    assert malformed.reasoning_content == ""

    anthropic_messages = llm.AnthropicProvider._convert_messages([
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "OpenAI-compatible extension",
            "tool_calls": [{
                "id": "tool-1",
                "type": "function",
                "function": {
                    "name": "weather",
                    "arguments": '{"city":"Tokyo"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "tool-1", "content": '{"ok":true}'},
    ])
    assert anthropic_messages[0]["content"][0]["input"] == {"city": "Tokyo"}
    assert "reasoning_content" not in anthropic_messages[0]
    assert anthropic_messages[1]["content"][0]["tool_use_id"] == "tool-1"

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

    from mochi.tool_availability import ToolAvailability
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

    from mochi.skills.base import Skill, SkillContext, SkillResult
    from mochi.tool_availability import tool_call_error
    from mochi.tool_execution import model_result_for, outcome_for

    pre_dispatch = json.loads(tool_call_error(
        "manage_todo",
        "invalid_tool_arguments",
        "arguments.todo_id is required",
    ))
    assert pre_dispatch == {
        "ok": False,
        "code": "invalid_tool_arguments",
        "started": False,
        "retryable": True,
        "changed": False,
        "message": "arguments.todo_id is required",
    }

    class _SemanticFailureSkill(Skill):
        async def execute(self, context):
            return SkillResult(
                output="todo_id is required",
                success=False,
                error_code="invalid_arguments",
                retryable=True,
            )

    semantic_failure = asyncio.run(_SemanticFailureSkill().run(SkillContext(
        trigger="tool_call",
        tool_name="manage_todo",
    )))
    assert json.loads(model_result_for(semantic_failure)) == {
        "ok": False,
        "code": "invalid_arguments",
        "started": True,
        "retryable": True,
        "changed": False,
        "message": "todo_id is required",
    }

    class _ExplodingSkill(Skill):
        async def execute(self, context):
            raise RuntimeError("write outcome unknown")

    execution_failure = asyncio.run(_ExplodingSkill().run(SkillContext(
        trigger="tool_call",
        tool_name="example_write",
    )))
    uncertain = json.loads(model_result_for(execution_failure))
    assert uncertain == {
        "ok": False,
        "code": "skill_exception",
        "started": True,
        "retryable": False,
        "message": "Skill error: write outcome unknown",
    }
    assert "changed" not in uncertain

    successful_mutation = SkillResult(
        output="Error-shaped prose is still only prose.",
        state_changed=True,
        execution_started=True,
    )
    assert json.loads(model_result_for(successful_mutation)) == {
        "ok": True,
        "changed": True,
        "result": "Error-shaped prose is still only prose.",
    }
    assert "source" not in json.loads(model_result_for(successful_mutation))
    assert outcome_for(
        "example",
        "example_write",
        {},
        successful_mutation,
    )["state_changed"] is True

    import mochi.skills.web_search.handler as web_handler
    from mochi.skills.web_search.handler import WebSearchSkill

    async def _search(*args, **kwargs):
        return "1. External result"

    monkeypatch.setattr(web_handler, "_bing_search", _search)
    web_success = asyncio.run(WebSearchSkill().run(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "Mochi"},
    )))
    assert json.loads(model_result_for(web_success)) == {
        "ok": True,
        "source": "external_web",
        "authority": "untrusted_data",
        "result": "1. External result",
    }

    calls = []

    async def _baidu(query, *, api_key, max_results, recency, **_kwargs):
        calls.append(("baidu", query, api_key, max_results, recency))
        return "1. Baidu result"

    monkeypatch.setattr(web_handler, "_baidu_search", _baidu)
    configured_search = WebSearchSkill()
    configured_search.config = {"BAIDU_API_KEY": "secret"}
    baidu_success = asyncio.run(configured_search.run(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "today", "max_results": 3, "recency": "week"},
    )))
    assert baidu_success.output == "1. Baidu result"
    assert calls == [("baidu", "today", "secret", 3, "week")]

    async def _failed_baidu(*_args, **_kwargs):
        raise ValueError("Baidu API key was rejected.")

    monkeypatch.setattr(web_handler, "_baidu_search", _failed_baidu)
    baidu_fallback = asyncio.run(configured_search.run(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "today", "recency": "week"},
    )))
    assert baidu_fallback.success
    assert "Bing fallback" in baidu_fallback.output
    assert baidu_fallback.output.endswith("1. External result")

    async def _failed_search(*args, **kwargs):
        raise ValueError("network unavailable")

    monkeypatch.setattr(web_handler, "_bing_search", _failed_search)
    web_failure = asyncio.run(WebSearchSkill().run(SkillContext(
        trigger="tool_call",
        tool_name="web_search",
        args={"query": "Mochi"},
    )))
    failed_web_payload = json.loads(model_result_for(web_failure))
    assert "source" not in failed_web_payload
    assert "authority" not in failed_web_payload

    import mochi.skills.habit.handler as habit_handler
    from mochi.skills.habit.handler import HabitSkill

    habit = {
        "id": 7,
        "name": "Read",
        "paused_until": "2026-09-10",
    }
    original_list_habits = habit_handler.list_habits
    monkeypatch.setattr(habit_handler, "list_habits", lambda _user_id: [habit])
    monkeypatch.setattr(habit_handler, "pause_habit", lambda *_args: True)
    no_op = HabitSkill()._pause(1, {
        "habit_id": 7,
        "until": "2026-09-10",
    })
    assert no_op.success
    assert not no_op.state_changed

    from mochi.skills.habit.queries import add_habit

    monkeypatch.setattr(habit_handler, "list_habits", original_list_habits)
    habit_id = add_habit(1, "Walk", "daily:1")
    first_remove = HabitSkill()._remove(1, {"habit_id": habit_id})
    repeated_remove = HabitSkill()._remove(1, {"habit_id": habit_id})
    assert first_remove.state_changed
    assert repeated_remove.success
    assert not repeated_remove.state_changed

    import mochi.skills.sticker.handler as sticker_handler
    import mochi.skills.sticker.queries as sticker_queries
    from mochi.skills.sticker.handler import StickerSkill

    monkeypatch.setattr(
        sticker_handler,
        "get_last_sent_sticker",
        lambda _chat_id: "sticker-file",
    )
    monkeypatch.setattr(
        sticker_queries,
        "delete_sticker",
        lambda _file_id: True,
    )
    assert StickerSkill._delete_last(1).state_changed
