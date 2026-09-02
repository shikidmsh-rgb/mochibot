"""Essential autonomous Main behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import chat
from mochi.core_store import replace_core
from mochi.db import _connect, get_recent_messages, save_message
from mochi.heartbeat_runtime import set_schedule_due
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry
from tests.e2e.mock_llm import make_response


def test_observer_rediscovery_preserves_runtime_cache():
    import mochi.observers as observers

    assert len(observers.discover()) == 6
    original = observers.get_observer("time_context")
    original._last_data = {"date": "cache-marker"}
    original._last_collected_at = datetime(
        2026, 8, 15, 12, 0, tzinfo=timezone.utc,
    )

    assert len(observers.discover()) == 6
    assert observers.get_observer("time_context") is original
    assert original._last_data == {"date": "cache-marker"}


@pytest.mark.asyncio
async def test_free_time_keeps_only_immediate_conversation_context(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.ai_client as ai_client

    replace_core("CORE_MARKER", source="test")
    monkeypatch.setattr(
        ai_client,
        "_retrieve_memories_for_turn",
        lambda *args: pytest.fail("Free Time must not auto-recall"),
    )
    for number in range(3):
        turn_id = f"free-time-history-{number}"
        save_message(1, "user", f"user-{number}", turn_id=turn_id)
        save_message(1, "assistant", f"assistant-{number}", turn_id=turn_id)
    save_message(1, "user", "unpaired-user-message", turn_id="incomplete-turn")
    mock = mock_llm_factory([make_response("[SKIP]")])
    entry = MainRuntimeEntry.free_time(
        run_key="free_time:test",
        wake_reason="periodic",
        user_id=1,
        channel_id=100,
        transport="fake",
        claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    )

    result = await chat(runtime_entry=entry)

    prompt = mock.call_log[0]["messages"][0]["content"]
    assert result.disposition == "skip"
    assert "CORE_MARKER" in prompt
    assert "用户上次发消息：" in prompt
    history = mock.call_log[0]["messages"][1:]
    assert [item["role"] for item in history] == [
        "user", "assistant", "user", "assistant",
    ]
    assert [item["content"].split("] ", 1)[-1] for item in history] == [
        "user-1", "assistant-1", "user-2", "assistant-2",
    ]


@pytest.mark.asyncio
async def test_failed_proactive_delivery_reuses_prepared_result(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    clock = {"now": datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(runtime, "_utc_now", lambda: clock["now"])

    async def no_observer_change():
        return False

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    monkeypatch.setattr(heartbeat, "ensure_daily_free_time_plan", lambda **_: [])
    monkeypatch.setattr(heartbeat, "_state", heartbeat.AWAKE)
    mock = mock_llm_factory([make_response("I was thinking of you.")])
    deliveries = [False, True]
    prepared = 0
    budget_checks = 0

    def delivery_wait(**_kwargs):
        nonlocal budget_checks
        budget_checks += 1
        if budget_checks > 1:
            pytest.fail("prepared transport retry must bypass proactive budget")
        return 0

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return await chat(runtime_entry=entry)

    async def deliver(_channel_id, _result):
        return deliveries.pop(0)

    monkeypatch.setattr(heartbeat, "delivery_wait_seconds", delivery_wait)
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", clock["now"])
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])

    assert prepared == 1
    assert budget_checks == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1)[0]["content"] == "I was thinking of you."


@pytest.mark.asyncio
async def test_proactive_cooldown_suppresses_instead_of_queuing(
    mock_llm_factory,
    monkeypatch,
):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    clock = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: clock)
    monkeypatch.setattr(heartbeat, "delivery_wait_seconds", lambda **_: 1800)

    async def no_observer_change():
        return False

    async def deliver(_channel_id, _result):
        pytest.fail("suppressed proactive result must not be delivered")

    async def prepare(entry):
        return await chat(runtime_entry=entry)

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    monkeypatch.setattr(heartbeat, "ensure_daily_free_time_plan", lambda **_: [])
    monkeypatch.setattr(heartbeat, "_state", heartbeat.AWAKE)
    mock_llm_factory([make_response("A time-sensitive thought.")])
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", clock)

    await heartbeat.run_main_runtime_tick(1, now=clock)

    conn = _connect()
    row = conn.execute(
        "SELECT status, outcome, next_attempt_at, last_error FROM heartbeat_runs "
        "WHERE entry_kind = 'free_time' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "status": "delivered",
        "outcome": "suppressed",
        "next_attempt_at": None,
        "last_error": "",
    }
    assert get_recent_messages(1) == []

    conn = _connect()
    conn.execute(
        "INSERT INTO heartbeat_runs "
        "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
        "facts_json, status, result_json, outcome, next_attempt_at, last_error, "
        "created_at) VALUES (?, 'free_time', 1, 100, 'fake', 'periodic', '[]', "
        "'ready', ?, 'ready', ?, 'delivery budget/cooldown', ?)",
        (
            "free_time:legacy-budget-queue",
            DurableChatResult(
                text="This stale thought must not be delivered.",
                disposition="deliver",
            ).to_json(),
            "2099-01-01T00:00:00+00:00",
            "2026-08-10T12:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    await heartbeat.run_main_runtime_tick(1, now=clock)

    conn = _connect()
    legacy = conn.execute(
        "SELECT status, outcome, next_attempt_at FROM heartbeat_runs "
        "WHERE run_key = 'free_time:legacy-budget-queue'"
    ).fetchone()
    conn.close()
    assert dict(legacy) == {
        "status": "delivered",
        "outcome": "suppressed",
        "next_attempt_at": None,
    }
