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
from tests.e2e.mock_llm import make_response


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
