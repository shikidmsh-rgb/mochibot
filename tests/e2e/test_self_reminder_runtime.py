"""One durable Self Reminder delivery path."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import (
    ChatResult,
    _render_completed_conversation_evidence,
    chat,
)
from mochi.main_runtime import MainRuntimeEntry
from mochi.db import (
    _connect,
    finish_tool_execution,
    get_recent_messages,
    save_message,
    start_tool_execution,
)
from mochi.reminder_timer import (
    _fire_reminder,
    set_self_reminder_callbacks,
    set_send_callback,
)
from mochi.skills.reminder.queries import (
    create_reminder,
    create_self_reminder,
    get_schedulable_reminders,
)
from tests.e2e.mock_llm import make_response


@pytest.mark.asyncio
async def test_request_separates_completed_chat_from_typed_event(
    mock_llm_factory,
):
    save_message(1, "user", "We were just talking about bananas.", turn_id="fruit")
    save_message(
        1, "assistant", "The ripe ones are best.", turn_id="fruit",
    )
    mock = mock_llm_factory([make_response("[SKIP]")])
    entry = MainRuntimeEntry.self_reminder(
        reminder_id=7,
        scheduled_for="2026-08-13T01:00:00+00:00",
        intent="Check whether the user's father got home safely.",
        user_id=1,
        channel_id=100,
        transport="fake",
        claim_token="claim",
        lease_until="2026-08-13T01:05:00+00:00",
        recurrence="daily",
    )

    result = await chat(runtime_entry=entry)

    messages = mock.call_log[0]["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    base_context, event = (message["content"] for message in messages)
    assert "最近已完成对话（只读证据）" in base_context
    assert "talking about bananas" in base_context
    assert "<self_reminder_event>" not in base_context
    assert "<self_reminder_event>" in event
    assert sum(
        message["content"].count("<self_reminder_event>")
        for message in messages
    ) == 1
    assert "new_user_message: false" in event
    assert "father got home safely" in event
    assert "scheduled_for: 2026-08-13T01:00:00+00:00" in event
    assert "recurrence: daily" in event
    assert "bananas" not in event
    tool_names = {
        tool["function"]["name"]
        for tool in mock.call_log[0]["tools"]
    }
    assert {"request_tools", "schedule_self_reminder"} <= tool_names
    assert result.disposition == "skip"


def test_completed_conversation_evidence_is_bounded():
    rendered = _render_completed_conversation_evidence([
        {
            "role": "user",
            "content": f"old-{number}-" + ("x" * 900),
            "created_at": f"2026-08-12T0{number}:00:00+00:00",
        }
        for number in range(9)
    ])

    assert len(rendered) < 7000
    assert '"order":"chronological_recent_window"' in rendered
    assert '"truncated":true' in rendered
    assert "old-8-" in rendered
    assert "old-0-" not in rendered
    assert rendered.index("old-3-") < rendered.index("old-8-")


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

    async def deliver(_channel_id, _result):
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
async def test_recurring_notify_advances_same_row(monkeypatch):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])

    due = clock["now"] - timedelta(days=3, minutes=1)
    reminder_id = create_reminder(
        1,
        100,
        "drink water",
        due.isoformat(),
        "daily",
    )

    async def rephrase(_message, _user_id):
        return "Drink water."

    delivered = []

    async def send(_user_id, text):
        delivered.append(text)
        return True

    monkeypatch.setattr(timer, "_rephrase_reminder", rephrase)
    set_send_callback(send)
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    conn = _connect()
    row = conn.execute(
        "SELECT id, status, remind_at, recurrence, prepared_text "
        "FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    assert delivered == ["Drink water."]
    assert row["status"] == "pending"
    expected_next = due + timedelta(days=4)
    assert row["remind_at"] == expected_next.isoformat()
    assert datetime.fromisoformat(row["remind_at"]) > clock["now"]
    assert row["recurrence"] == "daily"
    assert row["prepared_text"] is None


@pytest.mark.asyncio
async def test_recurring_self_advances_after_silent_outcome(
    monkeypatch,
):
    import mochi.reminder_timer as timer
    import mochi.skills.reminder.queries as queries

    clock = {"now": datetime(2026, 8, 12, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    due = clock["now"] - timedelta(minutes=1)
    reminder_id = create_self_reminder(
        1,
        100,
        "review hydration progress",
        due.isoformat(),
        "fake",
        "daily",
    )

    seen_recurrence = []

    async def prepare(entry):
        seen_recurrence.append(entry.recurrence)
        return ChatResult(disposition="skip")

    async def deliver(_channel_id, _result):
        raise AssertionError("silent outcome must not cross transport")

    set_self_reminder_callbacks(prepare, deliver, "fake")
    await _fire_reminder(get_schedulable_reminders(now=clock["now"])[0])

    conn = _connect()
    row = conn.execute(
        "SELECT status, remind_at, recurrence, result_json, outcome "
        "FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    conn.close()
    assert row["status"] == "pending"
    assert row["remind_at"] == (due + timedelta(days=1)).isoformat()
    assert row["recurrence"] == "daily"
    assert row["result_json"] is None
    assert row["outcome"] is None
    assert seen_recurrence == ["daily"]
