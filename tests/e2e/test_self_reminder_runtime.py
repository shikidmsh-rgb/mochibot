"""One durable Self Reminder delivery path."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import chat
from mochi.db import get_recent_messages
from mochi.reminder_timer import _fire_reminder, set_self_reminder_callbacks
from mochi.skills.reminder.queries import (
    create_self_reminder,
    get_schedulable_reminders,
)
from tests.e2e.mock_llm import make_response, make_tool_call


@pytest.mark.asyncio
async def test_failed_delivery_reuses_prepared_main_result(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    create_self_reminder(
        1,
        100,
        "check whether to bring an umbrella",
        (clock["now"] - timedelta(minutes=1)).isoformat(),
        "fake",
    )
    mock = mock_llm_factory([make_response("Remember your umbrella tomorrow.")])
    deliveries = [False, True]
    prepared = 0

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return await chat(runtime_entry=entry)

    async def deliver(_channel_id, _result, *, can_deliver):
        assert can_deliver()
        return deliveries.pop(0)

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    assert prepared == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1)[0]["content"] == (
        "Remember your umbrella tomorrow."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["deliver", "no_op", "handled", "interrupted"])
async def test_recurring_self_advances_every_terminal_outcome(
    mock_llm_factory, monkeypatch, outcome,
):
    from mochi.db import _connect, start_tool_execution, finish_tool_execution
    from mochi.skills.todo.queries import get_todos
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    now = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(timer, "_utc_now", lambda: now)
    monkeypatch.setattr(queries, "_now", lambda: now)
    rid = create_self_reminder(1, 100, "Review progress", now.isoformat(), "fake", "weekdays")
    responses = [make_response("Check in." if outcome == "deliver" else "[SKIP]")]
    if outcome == "handled":
        responses.insert(0, make_response(tool_calls=[make_tool_call(
            "manage_todo", {"action": "add", "task": "Bring an umbrella"},
        )]))
        responses.insert(0, make_response(tool_calls=[make_tool_call(
            "request_tools", {"skills": ["todo"]},
        )]))
    if outcome == "interrupted":
        eid = start_tool_execution(
            turn_id=f"self-reminder:{rid}:{now.isoformat()}",
            tool_call_id="interrupted", user_id=1, source="runtime:self_reminder",
            skill_name="todo", tool_name="manage_todo", action="add", arguments_json="{}",
        )
        finish_tool_execution(eid, status="success", state_changed=True)
    mock = mock_llm_factory(responses)
    delivered = []

    async def prepare(entry):
        return await chat(runtime_entry=entry)

    async def deliver(_channel, result, *, can_deliver):
        assert can_deliver()
        delivered.append(result.text)
        return True

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=now)[0])
    conn = _connect()
    row = dict(conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())
    conn.close()
    assert row["status"] == "pending"
    assert row["remind_at"] == (now + timedelta(days=3)).isoformat()
    assert row["result_json"] is None and row["attempt_count"] == 0
    assert len(mock.call_log) == (0 if outcome == "interrupted" else 3 if outcome == "handled" else 1)
    assert delivered == (["Check in."] if outcome == "deliver" else [])
    assert len(get_todos(1)) == int(outcome == "handled")
    assert len(get_recent_messages(1)) == int(outcome == "deliver")


@pytest.mark.asyncio
async def test_self_component_retry_does_not_repeat_delivered_text(monkeypatch):
    from mochi.ai_client import ChatResult
    from mochi.db import _connect
    from mochi.main_runtime import DurableChatResult
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    rid = create_self_reminder(1, 100, "Check in", clock["now"].isoformat(), "fake", "daily")
    prepared = 0
    sent = []

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return ChatResult(
            text="Hello.", stickers=["test-sticker"],
            _pending_history={
                "user_id": 1, "content": "Hello.", "turn_id": "component-retry",
                "tool_history": None, "processed": True,
            },
        )

    async def deliver(_channel, result, *, can_deliver):
        assert can_deliver()
        sent.append((result.text, result.stickers))
        return len(sent) != 2

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])
    conn = _connect()
    stored = conn.execute("SELECT result_json FROM reminders WHERE id = ?", (rid,)).fetchone()[0]
    conn.close()
    assert DurableChatResult.from_json(stored).text == ""
    clock["now"] += timedelta(seconds=61)
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])
    assert prepared == 1
    assert sent == [("Hello.", []), ("", ["test-sticker"]), ("", ["test-sticker"])]
    assert len(get_recent_messages(1)) == 1
