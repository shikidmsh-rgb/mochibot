"""E2E tests for the chat flow: message → LLM → tool dispatch → DB → response."""

import json
from types import SimpleNamespace

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions, save_message
from mochi.main_runtime import MainRuntimeEntry
from tests.e2e.mock_llm import main_context, make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    """Helper to create an IncomingMessage."""
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake", owner_authorized=True,
    )


def test_history_dates_both_speakers_without_changing_stored_content(monkeypatch):
    from datetime import timezone
    import mochi.config as config
    from mochi.ai_client import _expand_history, _build_system_prompt, _WEEKDAY_NAMES

    monkeypatch.setattr(config, "TZ", timezone.utc)
    history = [
        {"role": "user", "content": "Hello", "created_at": "2025-01-02T03:04:00+00:00"},
        {
            "role": "assistant", "content": "Hi", "created_at": "2025-01-02T03:05:00+00:00",
            "reasoning_content": "private",
        },
    ]
    messages = _expand_history(history)
    assert messages[0]["content"] == "Hello"
    assert messages[1]["content"] == "Hi"
    assert messages[1]["reasoning_content"] == "private"
    assert history[1]["content"] == "Hi"
    from mochi.ai_client import _format_history_timestamps

    timestamps = _format_history_timestamps(history)
    assert timestamps == "1. user: 2025-01-02 03:04\n2. assistant: 2025-01-02 03:05"
    prompt = _build_system_prompt(1, history_timestamps=timestamps)
    assert timestamps in prompt
    assert "{{history_timestamps}}" not in prompt
    assert any(name in prompt for name in _WEEKDAY_NAMES)

    history[1]["content"] = "[2025-01-02 03:05] [2025-01-02 03:05] Hi"
    assert _expand_history(history)[1]["content"] == history[1]["content"]


def test_current_image_preserves_history_time_indices():
    from mochi.ai_client import _expand_history, _image_content, _replace_current_user_content
    from mochi.transport import ImageAttachment

    history = [
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "user", "content": "[图片] Hello"},
    ]
    messages = [{"role": "system", "content": "Context"}, *_expand_history(history)]
    image = ImageAttachment(data=b"image")
    _replace_current_user_content(messages, "[图片] Hello", _image_content("Hello", image))
    assert len(messages) == 3
    assert messages[1] == history[0]
    assert messages[2] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "image_url", "image_url": {"url": image.data_url(), "detail": "auto"}},
        ],
    }


@pytest.mark.asyncio
async def test_image_goes_to_main_but_not_persistent_history(mock_llm_factory):
    from mochi.db import get_recent_messages
    from mochi.transport import ImageAttachment

    mock = mock_llm_factory([make_response("I see it.")])
    message = IncomingMessage(
        user_id=1, channel_id=1, transport="wechat", owner_authorized=True,
        text="用户发来一张图片。",
        image=ImageAttachment(data=b"\x89PNG\r\n\x1a\n", media_type="image/png"),
    )
    await chat(message)
    assert mock.requested_tiers == ["main"]
    assert mock.call_log[0]["messages"][-1]["content"] == [
        {"type": "text", "text": message.text},
        {
            "type": "image_url",
            "image_url": {"url": message.image.data_url(), "detail": "auto"},
        },
    ]
    stored = get_recent_messages(1)
    assert stored[-1]["content"] == "[图片] 用户发来一张图片。"
    assert not stored[-1].get("image_data")
    assert message.image.data_url() not in json.dumps(stored)


@pytest.mark.asyncio
async def test_file_text_goes_to_main_but_not_persistent_history(mock_llm_factory):
    from mochi.db import get_recent_messages
    from mochi.transport import FileAttachment

    mock = mock_llm_factory([make_response("Read it."), make_response("Hmm.")])
    message = IncomingMessage(
        user_id=1, channel_id=1, transport="wechat", owner_authorized=True,
        text="用户发来文件「plan.txt」。",
        file=FileAttachment(name="plan.txt", data="周末去爬山".encode()),
    )
    await chat(message)
    assert mock.call_log[0]["messages"][-1]["content"] == (
        "用户发来文件「plan.txt」。\n\n以下是文件「plan.txt」中的文字：\n周末去爬山"
    )
    stored = get_recent_messages(1)
    assert stored[-1]["content"] == "[文件] 用户发来文件「plan.txt」。"
    assert "周末去爬山" not in json.dumps(stored)

    await chat(IncomingMessage(
        user_id=1, channel_id=1, transport="wechat", owner_authorized=True,
        text="用户发来文件「a.zip」。", file=FileAttachment(name="a.zip", data=b"PK\x00\x00"),
    ))
    assert mock.call_log[1]["messages"][-1]["content"].endswith(
        "（这个文件的格式暂时读不了，只知道文件名。）"
    )


class TestSimpleReply:
    """LLM returns a plain text reply — no tool calls."""

    @pytest.mark.asyncio
    async def test_simple_reply(self, mock_llm_factory):
        save_message(1, "user", "Earlier message")
        save_message(1, "assistant", "Earlier reply")
        mock = mock_llm_factory([make_response("Hello there!"), make_response("Again!")])

        (await chat(_msg("Hi"))).confirm_delivered(final=True)
        await chat(_msg("Again"))

        first, second = (call["messages"] for call in mock.call_log)
        assert first[1:3] == [
            {"role": "user", "content": "Earlier message"},
            {"role": "assistant", "content": "Earlier reply"},
        ]
        assert first[-1] == {"role": "user", "content": "Hi"}
        assert second[-1] == {"role": "user", "content": "Again"}
        # The cacheable prefix (system + earlier history) is identical across turns.
        assert second[0] == first[0]
        assert second[1:4] == first[1:3] + [first[-1]]
        assert [m["role"] for m in second[4:]] == ["assistant", "user", "user"]
        turn_context = second[-2]["content"]
        assert turn_context.startswith("<turn_context")
        assert "4. assistant:" in turn_context and "5. user:" in turn_context
        assert "{{history_timestamps}}" not in turn_context
        assert "1. user:" not in first[0]["content"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_fails", [False, True])
    async def test_search_memory_counts_only_after_result_reaches_main(
        self, mock_llm_factory, monkeypatch, provider_fails,
    ):
        import mochi.ai_client as ai_client
        import mochi.config as config
        from mochi.db import _connect, save_memory_item

        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        monkeypatch.setattr(ai_client, "_retrieve_memories_for_turn", lambda *args: [])
        memory_id = save_memory_item(1, "Likes jasmine tea", source="admin")
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["internal_search"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("search_personal_history", {
                    "query": "jasmine", "source": "memory",
                }),
            ]),
            *(
                [RuntimeError("offline"), RuntimeError("offline")]
                if provider_fails else [make_response("Jasmine tea.")]
            ),
        ])
        await chat(_msg("Search my saved history."))
        outputs = [
            json.loads(message["content"])
            for message in mock.call_log[-1]["messages"] if message["role"] == "tool"
        ]
        assert outputs[-1]["ok"], outputs
        conn = _connect()
        references = conn.execute(
            "SELECT access_count FROM memory_items WHERE id=?", (memory_id,),
        ).fetchone()[0]
        conn.close()
        assert references == (0 if provider_fails else 1)

    @pytest.mark.asyncio
    async def test_habit_progress_context_appears_once_after_loading(
        self, mock_llm_factory, monkeypatch,
    ):
        import mochi.config as config
        from mochi.skills.habit.queries import add_habit

        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        add_habit(1, "Read", "daily:3")
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["habit"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("habit_progress", {"action": "list"}),
            ]),
            make_response("Your current reading progress."),
        ])
        await chat(_msg("How is my reading going?"))
        marker = "## 本轮习惯进度快照（只读事实）"
        contexts = [
            "\n".join(m["content"] for m in call["messages"] if m["role"] == "system")
            for call in mock.call_log
        ]
        assert [text.count(marker) for text in contexts] == [0, 1, 1]
        assert "0/3" in contexts[1]
        assert "daily:3" not in contexts[1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expiry", [None, "idle", "reset"])
    async def test_chat_session_carries_loaded_tools_in_stable_order(
        self, mock_llm_factory, monkeypatch, expiry,
    ):
        import mochi.config as config
        import mochi.heartbeat as heartbeat
        import mochi.turn_tool_policy as turn_tool_policy
        from mochi.db import set_context_reset

        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        monkeypatch.setattr(heartbeat, "bedtime_tool_available", lambda: False)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["habit"]}),
            ]),
            make_response("Loaded."),
            make_response("Follow-up."),
        ])
        await chat(_msg("Check my habits."))
        if expiry == "idle":
            started, reset_at, names = turn_tool_policy._session_toolboxes[1]
            turn_tool_policy._session_toolboxes[1] = (
                started - config.TOOL_SESSION_IDLE_MINUTES * 60 - 1,
                reset_at, names,
            )
        elif expiry == "reset":
            set_context_reset(1)
        await chat(_msg("And the second one?"))

        def tool_names(call):
            return [tool["function"]["name"] for tool in call["tools"]]

        if expiry is None:
            assert tool_names(mock.call_log[2]) == tool_names(mock.call_log[1])
            assert "habit_progress" in tool_names(mock.call_log[2])
        else:
            assert "habit_progress" not in tool_names(mock.call_log[2])

    @pytest.mark.asyncio
    async def test_slow_router_does_not_hold_main(self, monkeypatch):
        import asyncio
        import mochi.config as config
        import mochi.tool_router as tool_router

        async def slow_router(*_args, **_kwargs):
            await asyncio.sleep(1)
            return ["weather"]

        monkeypatch.setattr(tool_router, "classify_skills_llm", slow_router)
        monkeypatch.setattr(config, "TOOL_ROUTER_TIMEOUT_S", 0.01)
        assert await tool_router.classify_skills(
            "weather?", catalog={"weather": "Weather"},
        ) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_fails", [False, True])
    async def test_memory_references_count_exposure_once(
        self, mock_llm_factory, monkeypatch, provider_fails,
    ):
        import mochi.ai_client as ai_client
        from mochi.db import _connect, save_memory_item

        memory_id = save_memory_item(1, "Likes jasmine tea", source="admin")
        other_id = save_memory_item(2, "Other owner", source="admin")
        monkeypatch.setattr(
            ai_client, "_retrieve_memories_for_turn",
            lambda *args: [{"memory_id": memory_id, "text": "Likes jasmine tea"}],
        )
        responses = (
            [RuntimeError("offline"), RuntimeError("offline")]
            if provider_fails else [
                make_response(tool_calls=[
                    make_tool_call("request_tools", {"skills": ["weather"]}),
                ]),
                make_response("Jasmine tea."),
            ]
        )
        mock_llm_factory(responses)
        await chat(_msg("What tea do I like?"))
        conn = _connect()
        rows = dict(conn.execute(
            "SELECT id, access_count FROM memory_items",
        ).fetchall())
        conn.close()
        assert rows[memory_id] == (0 if provider_fails else 1)
        assert rows[other_id] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["chat", "self_reminder", "bedtime"])
    async def test_delivered_reasoning_is_private_and_model_scoped(
        self, mock_llm_factory, monkeypatch, kind,
    ):
        import mochi.ai_client as ai_client
        from mochi.db import get_conversation_context, get_recent_messages
        from mochi.main_runtime import DurableChatResult

        source = "https://api.deepseek.com/v1::model"
        response = make_response("Visible reply")
        response.reasoning_content = "Private provider reasoning"
        response.reasoning_source = source
        mock = mock_llm_factory([
            response, make_response("[SKIP]"), make_response("[SKIP]"),
        ])
        monkeypatch.setattr(
            type(mock), "reasoning_source", property(lambda self: source),
        )
        monkeypatch.setattr(
            ai_client, "_schedule_continuous_memory", lambda user_id: None,
        )
        if kind == "chat":
            result = await chat(_msg("hello"))
        else:
            result = await chat(runtime_entry=MainRuntimeEntry(
                kind=kind, user_id=1, channel_id=100, transport="fake",
                trigger="silence" if kind == "bedtime" else None,
            ))
        assert not any(
            message["role"] == "assistant" for message in get_recent_messages(1)
        )

        durable = DurableChatResult.from_json(result.to_durable().to_json())
        restored = ai_client.ChatResult.from_durable(durable)
        assert restored.confirm_delivered()
        assert not restored.confirm_delivered()
        public_history = get_recent_messages(1)
        assert public_history[-1]["content"] == "Visible reply"
        assert all("reasoning_content" not in message for message in public_history)
        assert all(
            "reasoning_content" not in message
            for message in get_conversation_context(1)["recent"]
        )

        next_entry = MainRuntimeEntry(
            kind="self_reminder", user_id=1, channel_id=100, transport="fake",
        )
        await chat(runtime_entry=next_entry)
        messages = mock.call_log[1]["messages"]
        replayed = next(
            message for message in messages if message["role"] == "assistant"
        )
        assert replayed["reasoning_content"] == response.reasoning_content
        assert all(
            response.reasoning_content not in (message.get("content") or "")
            for message in messages
        )

        monkeypatch.setattr(
            type(mock), "reasoning_source", property(lambda self: source + "-other"),
        )
        await chat(runtime_entry=next_entry)
        assert all(
            "reasoning_content" not in message for message in mock.call_log[2]["messages"]
        )

    @pytest.mark.asyncio
    async def test_main_can_request_bedtime(self, mock_llm_factory, monkeypatch):
        import mochi.heartbeat as heartbeat

        class EveningClock:
            @classmethod
            def now(cls, _tz):
                return SimpleNamespace(hour=21)

        monkeypatch.setattr(heartbeat, "datetime", EveningClock)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("enter_bedtime", {}),
            ]),
            make_response("Good night. I'll get some rest too."),
        ])

        reply = await chat(_msg("I'm heading to bed"))

        assert reply.bedtime_requested is True
        assert any(
            tool["function"]["name"] == "enter_bedtime"
            for tool in mock.call_log[0]["tools"]
        )

    @pytest.mark.asyncio
    async def test_silent_bedtime_does_not_repeat_recent_farewell(
        self, mock_llm_factory,
    ):
        turn_id = "recent-goodnight"
        save_message(1, "user", "晚安宝", turn_id=turn_id)
        save_message(1, "assistant", "晚安，睡吧。", turn_id=turn_id)
        mock = mock_llm_factory([make_response("[SKIP]")])
        entry = MainRuntimeEntry.bedtime(
            trigger="resleep",
            user_id=1,
            channel_id=100,
            transport="fake",
        )

        reply = await chat(runtime_entry=entry)

        assert reply.text == ""
        assert reply.disposition == "skip"
        assert len(mock.call_log) == 1
        assert any(
            "晚安，睡吧。" in message["content"]
            for message in mock.call_log[0]["messages"]
        )

    @pytest.mark.asyncio
    async def test_explicit_bedtime_setting_is_resident_and_applied(
        self, mock_llm_factory, monkeypatch,
    ):
        import mochi.admin.admin_db as admin_db
        import mochi.config as config
        import mochi.heartbeat as heartbeat
        import mochi.tool_router as tool_router

        monkeypatch.setattr(config, "TOOL_ROUTER_ENABLED", True)
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", False)
        monkeypatch.setattr(
            admin_db, "list_tier_assignments", lambda: {"lite": "mock"},
        )

        async def route_settings(*_args, **kwargs):
            return []

        monkeypatch.setattr(tool_router, "classify_skills", route_settings)
        admin_db.set_system_override("SLEEP_AFTER_HOUR", "21")

        class NightClock:
            @classmethod
            def now(cls, _tz):
                return SimpleNamespace(hour=22)

        monkeypatch.setattr(heartbeat, "datetime", NightClock)
        assert heartbeat._is_rest_hour(22) is True

        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("manage_settings", {
                    "action": "set",
                    "id": "runtime.sleep_after_hour",
                    "value": "23",
                }),
            ]),
            make_response("知道了，今晚十一点再休息。"),
        ])

        await chat(_msg("你睡觉时间太早了以后改成11点"))

        setting_tool = next(
            tool for tool in mock.call_log[0]["tools"]
            if tool["function"]["name"] == "manage_settings"
        )
        assert setting_tool["function"]["parameters"]["properties"]["value"]["type"] == "string"
        assert admin_db.get_system_config("SLEEP_AFTER_HOUR") == 23
        assert heartbeat._is_rest_hour(22) is False
        receipt = mock.call_log[1]["messages"][-1]
        assert receipt["role"] == "tool"
        assert '"changed":true' in receipt["content"]
        execution = get_recent_tool_executions(1, limit=1)[0]
        assert execution["tool_name"] == "manage_settings"
        assert execution["status"] == "success"
        assert execution["state_changed"] == 1

        from mochi.skills import dispatch

        denied = await dispatch(
            "manage_settings",
            {
                "action": "set",
                "id": "runtime.sleep_after_hour",
                "value": "22",
            },
            user_id=2,
            channel_id=200,
            transport="wechat",
            actor="main",
            owner_authorized=False,
        )
        assert denied.success is False
        assert denied.error_code == "user_authorization_required"
        assert admin_db.get_system_config("SLEEP_AFTER_HOUR") == 23

        locked = await dispatch(
            "manage_settings",
            {"action": "set", "id": "skills.skill_management.enabled", "value": "false"},
            user_id=1,
            actor="main",
            owner_authorized=True,
            source="chat",
        )
        assert locked.success is False
        assert locked.error_code == "locked_skill"

        from mochi.db import set_skill_enabled, set_skill_mode
        from mochi.turn_tool_policy import build_turn_tool_plan

        set_skill_enabled("skill_management", False)
        set_skill_mode("off")
        skilloff_plan = build_turn_tool_plan("fake")
        assert "manage_settings" in {
            tool["function"]["name"]
            for tool in skilloff_plan.resident_definitions
        }
        view_settings = await dispatch(
            "manage_settings",
            {"action": "list", "group": "runtime"},
            user_id=1,
            actor="main",
            owner_authorized=True,
        )
        assert view_settings.success is True

    @pytest.mark.asyncio
    async def test_update_core(self, mock_llm_factory):
        from mochi.db import get_recent_messages
        from mochi.core_store import read_core, replace_core
        replace_core("Core anchor")
        tool_response = make_response(tool_calls=[
                make_tool_call("update_core", {
                    "content": "Core anchor\n\nUser likes jasmine tea",
                }),
            ])
        tool_response.reasoning_content = "I should preserve this exact thought."
        mock = mock_llm_factory([
            tool_response,
            # Round 2: LLM gives final reply after tool result
            make_response("Got it, I'll remember that!"),
        ])

        reply = await chat(_msg("I really like jasmine tea"))

        assert "jasmine tea" in read_core()
        first_round_assistant = next(
            message for message in mock.call_log[1]["messages"]
            if message["role"] == "assistant" and "tool_calls" in message
        )
        assert first_round_assistant["reasoning_content"] == (
            "I should preserve this exact thought."
        )
        reply.confirm_delivered()
        persisted = get_recent_messages(1, limit=10)
        assert all(
            "I should preserve this exact thought." not in message["content"]
            for message in persisted
        )

        unchanged_core = read_core()
        rejected_calls = [
            (
                {
                    "id": "malformed",
                    "name": "update_core",
                    "arguments": None,
                    "argument_error": "arguments were not valid JSON",
                },
                True,
                "malformed_tool_arguments",
            ),
            (
                make_tool_call("update_core", {
                    "content": "must not run",
                }, call_id="incomplete"),
                False,
                "incomplete_tool_call",
            ),
            (
                make_tool_call("update_core", {}, call_id="required"),
                True,
                "invalid_tool_arguments",
            ),
            (
                make_tool_call(
                    "update_core", {"content": 1}, call_id="type",
                ),
                True,
                "invalid_tool_arguments",
            ),
            (
                make_tool_call(
                    "write_diary", {"content": "must not run", "day": "invent"},
                    call_id="enum",
                ),
                True,
                "invalid_tool_arguments",
            ),
        ]
        for index, (tool_call, complete, expected_error) in enumerate(
            rejected_calls, start=2,
        ):
            attempted = make_response(tool_calls=[tool_call])
            attempted.tool_calls_complete = complete
            mock = mock_llm_factory([
                attempted,
                make_response(f"Recovered {expected_error}"),
            ])

            await chat(_msg(f"invalid call {index}", user_id=index))

            model_error = json.loads(
                mock.call_log[1]["messages"][-1]["content"]
            )
            assert model_error["code"] == expected_error
            assert model_error["started"] is False
            assert model_error["retryable"] is True
            assert model_error["changed"] is False
            assistant_messages = [
                message for message in mock.call_log[1]["messages"]
                if message["role"] == "assistant" and "tool_calls" in message
            ]
            assert all(
                "reasoning_content" not in message
                for message in assistant_messages
            )
            assert read_core() == unchanged_core

    @pytest.mark.asyncio
    async def test_complete_document_snapshots_advance_between_rounds(self, mock_llm_factory):
        from mochi.core_store import replace_core, read_core
        from mochi.diary import diary
        replace_core("Original")
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("update_core", {"content": "First"}),
                make_tool_call("write_diary", {"content": "First journal"}),
            ]),
            make_response(tool_calls=[
                make_tool_call("update_core", {"content": "Second"}),
                make_tool_call("write_diary", {"content": "Second journal"}),
            ]),
            make_response("Done"),
        ])
        await chat(_msg("Revise these documents"))
        assert read_core() == "Second"
        assert diary.read("今日日記") == "Second journal"
        for call in mock.call_log:
            for message in call["messages"]:
                if message.get("tool_calls"):
                    assert "_expected_content" not in json.dumps(message["tool_calls"])

class TestToolCallReminder:
    """LLM calls manage_reminder tool."""

    @pytest.mark.asyncio
    async def test_create_reminder(self, mock_llm_factory, monkeypatch):
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["reminder"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("manage_reminder", {
                    "action": "create",
                    "message": "Take a break",
                    "remind_at": "2099-01-01T12:00:00",
                }),
            ]),
            make_response("Reminder set!"),
        ])

        await chat(_msg("Remind me to take a break"))

        # Reminder is in the future, so it won't show in get_pending_reminders
        # (which filters remind_at <= now). Verify via direct DB query.
        from mochi.db import _connect
        conn = _connect()
        rows = conn.execute(
            "SELECT message FROM reminders WHERE fired = 0"
        ).fetchall()
        conn.close()
        assert any("Take a break" in r[0] for r in rows)

        executions = get_recent_tool_executions(1)
        assert len(executions) == 1
        assert executions[0]["tool_name"] == "manage_reminder"
        assert executions[0]["arguments"]["message"] == "Take a break"
        assert executions[0]["status"] == "success"
        assert executions[0]["state_changed"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("followup", [
        "把刚才那个改成后天",
        "Continue finishing the original task.",
    ])
    async def test_followup_gets_real_receipt_without_replayed_tool_protocol(
        self, mock_llm_factory, monkeypatch, followup,
    ):
        import mochi.ai_client as ai_client
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        monkeypatch.setattr(ai_client, "_schedule_continuous_memory", lambda _user: None)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["reminder"]}),
            ]),
            make_response(tool_calls=[
                make_tool_call("manage_reminder", {
                    "action": "create",
                    "message": "Submit report",
                    "remind_at": "2099-01-01T12:00:00",
                }),
            ]),
            make_response("Reminder set!"),
            make_response("Okay, I'll change it."),
        ])

        first = await chat(_msg("Remind me to submit the report"))
        first.confirm_delivered()
        await chat(_msg(followup))

        followup_messages = mock.call_log[3]["messages"]
        system_prompt = main_context(followup_messages)
        assert "Reminder #" in system_prompt
        assert "Submit report" in system_prompt
        assert all(message["role"] != "tool" for message in followup_messages)
        assert all(
            "tool_calls" not in message for message in followup_messages
            if message["role"] == "assistant"
        )
