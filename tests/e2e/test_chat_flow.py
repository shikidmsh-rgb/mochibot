"""E2E tests for the chat flow: message → LLM → tool dispatch → DB → response."""

import json

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions, save_message
from mochi.main_runtime import MainRuntimeEntry
from mochi.skills.todo.queries import get_todos
from tests.e2e.mock_llm import make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    """Helper to create an IncomingMessage."""
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake",
    )


class TestSimpleReply:
    """LLM returns a plain text reply — no tool calls."""

    @pytest.mark.asyncio
    async def test_simple_reply(self, mock_llm_factory):
        mock = mock_llm_factory([make_response("Hello there!")])

        reply = await chat(_msg("Hi"))

        assert reply.text == "Hello there!"
        assert len(mock.call_log) == 1

    @pytest.mark.asyncio
    async def test_main_can_request_bedtime(self, mock_llm_factory, monkeypatch):
        import mochi.heartbeat as heartbeat

        monkeypatch.setattr(heartbeat, "bedtime_tool_available", lambda: True)
        mock = mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("enter_bedtime", {}),
            ]),
            make_response("Good night. I'll get some rest too."),
        ])

        reply = await chat(_msg("I'm heading to bed"))

        assert reply.bedtime_requested is True
        assert reply.text == "Good night. I'll get some rest too."
        assert any(
            tool["function"]["name"] == "enter_bedtime"
            for tool in mock.call_log[0]["tools"]
        )
        assert "Bedtime will begin after your farewell" in (
            mock.call_log[1]["messages"][-1]["content"]
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
        assert "如果刚刚已经完成睡前告别，你可以只回复 `[SKIP]`" in (
            mock.call_log[0]["messages"][0]["content"]
        )
        assert any(
            message["role"] == "assistant" and "晚安，睡吧。" in message["content"]
            for message in mock.call_log[0]["messages"]
        )

    @pytest.mark.asyncio
    async def test_update_core(self, mock_llm_factory):
        from mochi.db import get_recent_messages
        from mochi.core_store import read_core, replace_core
        replace_core("Core anchor")
        tool_response = make_response(tool_calls=[
                make_tool_call("update_core", {
                    "action": "insert_after",
                    "anchor_text": "Core anchor",
                    "content": "User likes jasmine tea",
                }),
            ])
        tool_response.reasoning_content = "I should preserve this exact thought."
        mock = mock_llm_factory([
            tool_response,
            # Round 2: LLM gives final reply after tool result
            make_response("Got it, I'll remember that!"),
        ])

        reply = await chat(_msg("I really like jasmine tea"))

        assert "remember" in reply.text.lower()
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
                    "action": "insert_after",
                    "anchor_text": "Core anchor",
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
                    "update_core", {"action": 1}, call_id="type",
                ),
                True,
                "invalid_tool_arguments",
            ),
            (
                make_tool_call(
                    "update_core", {"action": "invent"}, call_id="enum",
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

            recovered = await chat(_msg(f"invalid call {index}", user_id=index))

            assert expected_error in recovered.text
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

        reply = await chat(_msg("Remind me to take a break"))

        assert "reminder" in reply.text.lower() or "set" in reply.text.lower()
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
    async def test_followup_gets_real_receipt_without_replayed_tool_protocol(
        self, mock_llm_factory, monkeypatch,
    ):
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
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

        await chat(_msg("Remind me to submit the report"))
        await chat(_msg("把刚才那个改成后天"))

        followup_messages = mock.call_log[3]["messages"]
        system_prompt = followup_messages[0]["content"]
        assert "最近已确认的系统操作" in system_prompt
        assert "Reminder #" in system_prompt
        assert "Submit report" in system_prompt
        assert all(message["role"] != "tool" for message in followup_messages)
        assert all(
            "tool_calls" not in message for message in followup_messages
            if message["role"] == "assistant"
        )


class TestMultiToolLoop:
    """LLM makes multiple sequential tool calls across rounds."""

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self, mock_llm_factory, monkeypatch):
        """Single LLM response with multiple tool_calls."""
        import mochi.config as config
        monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("request_tools", {"skills": ["todo"]}),
            ]),
            # The requested tool becomes available only in the next round.
            make_response(tool_calls=[
                make_tool_call("manage_todo", {
                    "action": "add",
                    "task": "Research hiking trails",
                }),
            ]),
            # Final reply after both tool results.
            make_response("Noted your hobby and added a todo!"),
        ])

        reply = await chat(_msg("I like hiking, add research trails to my list"))

        assert "noted" in reply.text.lower() or "todo" in reply.text.lower()
        todos = get_todos(1)
        assert any("hiking" in t["task"].lower() for t in todos)
