"""E2E tests for the chat flow: message → LLM → tool dispatch → DB → response."""

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from mochi.db import get_recent_messages, recall_memory, get_pending_reminders, get_todos
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
    async def test_reply_saved_to_db(self, mock_llm_factory):
        mock_llm_factory([make_response("Saved reply")])

        await chat(_msg("Test message"))

        msgs = get_recent_messages(1, limit=10)
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles
        assert any(m["content"] == "Saved reply" for m in msgs)

    @pytest.mark.asyncio
    async def test_conversation_history(self, mock_llm_factory):
        """Multiple messages build up conversation history."""
        mock_llm_factory([
            make_response("Reply 1"),
            make_response("Reply 2"),
            make_response("Reply 3"),
        ])

        await chat(_msg("First"))
        await chat(_msg("Second"))
        await chat(_msg("Third"))

        msgs = get_recent_messages(1, limit=20)
        assert len(msgs) == 6  # 3 user + 3 assistant


class TestToolCallMemory:
    """LLM calls save_memory tool, then replies."""

    @pytest.mark.asyncio
    async def test_save_memory(self, mock_llm_factory):
        mock_llm_factory([
            # Round 1: LLM decides to save a memory
            make_response(tool_calls=[
                make_tool_call("save_memory", {
                    "content": "User likes jasmine tea",
                    "category": "preference",
                }),
            ]),
            # Round 2: LLM gives final reply after tool result
            make_response("Got it, I'll remember that!"),
        ])

        reply = await chat(_msg("I really like jasmine tea"))

        assert "remember" in reply.text.lower()
        items = recall_memory(1)
        assert any("jasmine tea" in m["content"] for m in items)

    @pytest.mark.asyncio
    async def test_recall_memory(self, mock_llm_factory):
        """recall_memory tool returns saved memories."""
        mock_llm_factory([
            # First: save
            make_response(tool_calls=[
                make_tool_call("save_memory", {
                    "content": "prefers dark mode",
                    "category": "preference",
                }),
            ]),
            make_response("Noted!"),
            # Second conversation: recall
            make_response(tool_calls=[
                make_tool_call("recall_memory", {"query": "dark mode"}),
            ]),
            make_response("You prefer dark mode!"),
        ])

        await chat(_msg("I prefer dark mode"))
        reply = await chat(_msg("What do you know about my preferences?"))

        assert "dark mode" in reply.text.lower()


class TestToolCallReminder:
    """LLM calls manage_reminder tool."""

    @pytest.mark.asyncio
    async def test_create_reminder(self, mock_llm_factory):
        mock_llm_factory([
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


class TestToolCallTodo:
    """LLM calls manage_todo tool."""

    @pytest.mark.asyncio
    async def test_add_todo(self, mock_llm_factory):
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("manage_todo", {
                    "action": "add",
                    "task": "Buy groceries",
                }),
            ]),
            make_response("Added to your list!"),
        ])

        reply = await chat(_msg("Add buy groceries to my todo"))

        assert "list" in reply.text.lower() or "added" in reply.text.lower()
        todos = get_todos(1)
        assert any("Buy groceries" in t["task"] for t in todos)


class TestMultiToolLoop:
    """LLM makes multiple sequential tool calls across rounds."""

    @pytest.mark.asyncio
    async def test_sequential_tool_calls(self, mock_llm_factory):
        """LLM calls save_memory, then manage_todo, then replies."""
        mock_llm_factory([
            # Round 1: save memory
            make_response(tool_calls=[
                make_tool_call("save_memory", {
                    "content": "planning a trip",
                    "category": "event",
                }),
            ]),
            # Round 2: add todo
            make_response(tool_calls=[
                make_tool_call("manage_todo", {
                    "action": "add",
                    "task": "Pack bags for trip",
                }),
            ]),
            # Round 3: final reply
            make_response("Memory saved and todo added for your trip!"),
        ])

        reply = await chat(_msg("I'm planning a trip, remind me to pack"))

        assert "trip" in reply.text.lower()
        items = recall_memory(1)
        assert any("trip" in m["content"] for m in items)
        todos = get_todos(1)
        assert any("Pack" in t["task"] for t in todos)

    @pytest.mark.asyncio
    async def test_parallel_tool_calls(self, mock_llm_factory):
        """Single LLM response with multiple tool_calls."""
        mock_llm_factory([
            # Round 1: two tool calls in one response
            make_response(tool_calls=[
                make_tool_call("save_memory", {
                    "content": "likes hiking",
                    "category": "hobby",
                }),
                make_tool_call("manage_todo", {
                    "action": "add",
                    "task": "Research hiking trails",
                }),
            ]),
            # Round 2: final reply
            make_response("Noted your hobby and added a todo!"),
        ])

        reply = await chat(_msg("I like hiking, add research trails to my list"))

        assert "noted" in reply.text.lower() or "todo" in reply.text.lower()
        items = recall_memory(1)
        assert any("hiking" in m["content"] for m in items)
        todos = get_todos(1)
        assert any("hiking" in t["task"].lower() for t in todos)


class TestEdgeCases:
    """Edge cases and error paths."""

    @pytest.mark.asyncio
    async def test_max_rounds_exhausted(self, mock_llm_factory, monkeypatch):
        """When LLM keeps requesting tools beyond max rounds, return fallback."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 2)

        mock_llm_factory([
            # Round 1: tool call
            make_response(content="thinking...", tool_calls=[
                make_tool_call("save_memory", {
                    "content": "test",
                    "category": "test",
                }),
            ]),
            # Round 2: another tool call (hits limit)
            make_response(content="still thinking...", tool_calls=[
                make_tool_call("save_memory", {
                    "content": "test2",
                    "category": "test",
                }),
            ]),
        ])

        reply = await chat(_msg("test"))
        # Should get the last response content as fallback
        assert reply is not None
        assert len(reply.text) > 0
