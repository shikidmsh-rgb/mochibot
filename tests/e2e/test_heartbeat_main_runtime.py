"""Essential autonomous Main behavior."""

from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import ChatResult, chat
from mochi.core_store import replace_core
from mochi.db import _connect, get_recent_messages, save_message
from mochi.heartbeat_runtime import set_schedule_due
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry
from mochi.transport import DeliveryError, IncomingMessage
from tests.e2e.mock_llm import make_response, make_tool_call


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
@pytest.mark.parametrize("kind", ["chat", "free_time", "attention", "self_reminder", "bedtime"])
async def test_main_sees_delivered_autonomous_history_only(
    mock_llm_factory, kind,
):
    delivered = ChatResult(_pending_history={
        "user_id": 1, "content": "ALREADY_SAID_GOODNIGHT",
        "turn_id": "attention:delivered", "processed": True, "tool_history": None,
    })
    assert delivered.confirm_delivered()
    assert not delivered.confirm_delivered()
    ChatResult(_pending_history={
        "user_id": 1, "content": "UNSENT_DRAFT",
        "turn_id": "attention:failed", "processed": True, "tool_history": None,
    })
    save_message(2, "assistant", "OTHER_USER", processed=True)
    mock = mock_llm_factory([make_response("[SKIP]")])
    if kind == "chat":
        await chat(IncomingMessage(
            user_id=1, channel_id=100, text="hello", transport="fake",
        ))
    else:
        await chat(runtime_entry=MainRuntimeEntry(
            kind=kind, user_id=1, channel_id=100, transport="fake",
            trigger="silence" if kind == "bedtime" else None,
            intent="check in" if kind == "self_reminder" else None,
        ))

    messages = mock.call_log[0]["messages"]
    delivered_messages = [
        message for message in messages
        if "ALREADY_SAID_GOODNIGHT" in message.get("content", "")
    ]
    assert len(delivered_messages) == 1
    assert delivered_messages[0]["role"] == "assistant"
    assert delivered_messages[0]["content"].startswith("[")
    assert not any(
        value in message.get("content", "")
        for message in messages for value in ("UNSENT_DRAFT", "OTHER_USER")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("next_kind", ["free_time", "attention", "chat"])
async def test_silent_reminder_creation_is_visible_in_the_next_main_entry(
    mock_llm_factory, next_kind,
):
    from mochi.skills.reminder.queries import get_pending_reminders

    mock = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": ["reminder"]}),
        ]),
        make_response(tool_calls=[make_tool_call("manage_reminder", {
            "action": "create", "message": "BEDTIME_ALREADY_SCHEDULED",
            "remind_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        })]),
        make_response("[SKIP]"),
        make_response("[SKIP]"),
    ])
    first = await chat(runtime_entry=MainRuntimeEntry.free_time(
        run_key="free_time:silent-first", wake_reason="periodic", user_id=1,
        channel_id=100, transport="fake", claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    ))
    assert first.disposition == "handled"
    assert first.successful_effects
    assert get_recent_messages(1) == []
    assert len(get_pending_reminders()) == 1

    if next_kind == "chat":
        await chat(IncomingMessage(
            user_id=1, channel_id=100, text="hello", transport="fake",
        ))
    else:
        await chat(runtime_entry=MainRuntimeEntry(
            kind=next_kind, user_id=1, channel_id=100, transport="fake",
            run_key=f"{next_kind}:second", wake_reason="periodic",
        ))

    prompt = mock.call_log[3]["messages"][0]["content"]
    assert '"tool":"manage_reminder"' in prompt
    assert '"source":"runtime:free_time"' in prompt
    assert "BEDTIME_ALREADY_SCHEDULED" in prompt
    assert len(get_pending_reminders()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    False,
    DeliveryError("wechat: reply context missing", outcome="delivery_unavailable"),
    DeliveryError("wechat: ret=-1 errcode=42", outcome="delivery_rejected"),
    TimeoutError(),
])
async def test_failed_proactive_delivery_is_terminal(
    mock_llm_factory,
    monkeypatch,
    failure,
):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    clock = {"now": datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(runtime, "_utc_now", lambda: clock["now"])

    async def no_observer_change():
        return False

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    mock = mock_llm_factory([make_response("I was thinking of you.")])
    deliveries = 0
    prepared = 0
    budget_checks = 0

    def delivery_wait(**_kwargs):
        nonlocal budget_checks
        budget_checks += 1
        if budget_checks > 1:
            pytest.fail("a failed proactive turn must not run again")
        return 0

    async def prepare(entry):
        nonlocal prepared
        prepared += 1
        return await chat(runtime_entry=entry)

    async def deliver(_channel_id, _result, **_kwargs):
        nonlocal deliveries
        deliveries += 1
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(heartbeat, "delivery_wait_seconds", delivery_wait)
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", clock["now"])
    created = await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    for kind in ("free_time", "attention"):
        set_schedule_due(kind, clock["now"] + timedelta(days=2))
    clock["now"] += timedelta(days=1)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])

    assert prepared == 1
    assert budget_checks == 1
    assert deliveries == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1) == []
    conn = _connect()
    row = conn.execute(
        "SELECT status, outcome, attempt_count, next_attempt_at, last_error "
        "FROM heartbeat_runs WHERE run_key = ?", (created[0],),
    ).fetchone()
    conn.close()
    assert row["status"] == "failed"
    assert row["outcome"] == (
        failure.outcome if isinstance(failure, DeliveryError) else "delivery_unknown"
    )
    assert row["attempt_count"] == 1
    assert row["next_attempt_at"] is None
    assert row["last_error"]


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

    async def deliver(_channel_id, _result, **_kwargs):
        pytest.fail("suppressed proactive result must not be delivered")

    async def prepare(entry):
        return await chat(runtime_entry=entry)

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
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
        "status": "expired",
        "outcome": "expired",
        "next_attempt_at": None,
    }


@pytest.mark.asyncio
async def test_restart_expires_abandoned_turns_and_ignores_retired_clocks(monkeypatch):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)

    async def no_observer_change():
        return False

    async def unexpected(*args, **kwargs):
        pytest.fail("abandoned turns must not re-enter Main or delivery")

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    heartbeat.set_main_runtime_callbacks(unexpected, unexpected, "fake")
    set_schedule_due("free_time_plan", now - timedelta(days=1))
    old_result = DurableChatResult(text="Five thirty!", disposition="deliver").to_json()
    conn = _connect()
    for status in ("pending", "running", "ready"):
        conn.execute(
            "INSERT INTO heartbeat_runs "
            "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
            "status, result_json, created_at, lease_until, next_attempt_at, "
            "delivery_started_at, last_error) "
            "VALUES (?, 'free_time', 1, 100, 'fake', 'periodic', ?, ?, ?, ?, ?, ?, ?)",
            (
                status, status, old_result if status == "ready" else None,
                (now - timedelta(days=1)).isoformat(),
                (now - timedelta(hours=1)).isoformat() if status == "running" else None,
                (now + timedelta(days=1)).isoformat(),
                (now - timedelta(hours=1)).isoformat() if status == "ready" else None,
                "prior failure",
            ),
        )
    conn.commit()
    conn.close()

    assert await heartbeat.run_main_runtime_tick(1, now=now) == []
    conn = _connect()
    rows = conn.execute(
        "SELECT run_key, status, outcome, result_json, next_attempt_at, last_error "
        "FROM heartbeat_runs ORDER BY run_key"
    ).fetchall()
    conn.close()
    assert len(rows) == 3
    assert all(row["status"] == "expired" for row in rows)
    assert all(row["next_attempt_at"] is None for row in rows)
    assert all(row["last_error"] == "prior failure" for row in rows)
    ready = next(row for row in rows if row["run_key"] == "ready")
    assert ready["result_json"] == old_result
    assert ready["outcome"] == "delivery_unknown"
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["sleep", "sleep_wake", "lease"])
async def test_late_main_result_is_not_delivered(monkeypatch, interruption):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)
    monkeypatch.setattr(heartbeat, "delivery_wait_seconds", lambda **_: 0)

    async def no_observer_change():
        return False

    async def prepare(_entry):
        nonlocal now
        if interruption == "lease":
            now += timedelta(seconds=301)
        else:
            monkeypatch.setattr(heartbeat, "_state_changed_at", now)
            if interruption == "sleep":
                monkeypatch.setattr(heartbeat, "_state", heartbeat.SLEEPING)
        return ChatResult(text="An old thought")

    async def deliver(*args, **kwargs):
        pytest.fail("late Main result must not be delivered")

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", now)
    created = await heartbeat.run_main_runtime_tick(1, now=now)
    conn = _connect()
    row = conn.execute(
        "SELECT outcome, next_attempt_at FROM heartbeat_runs WHERE run_key = ?",
        (created[0],),
    ).fetchone()
    conn.close()
    assert row["outcome"] == "expired"
    assert row["next_attempt_at"] is None


@pytest.mark.asyncio
async def test_delivered_text_survives_sticker_failure(monkeypatch, mock_llm_factory):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)
    monkeypatch.setattr(heartbeat, "delivery_wait_seconds", lambda **_: 0)

    async def no_observer_change():
        return False

    async def prepare(entry):
        result = await chat(runtime_entry=entry)
        result.stickers = ["sticker"]
        return result

    delivered = []

    async def deliver(_channel, result, **_kwargs):
        delivered.append(result)
        return bool(result.text)

    monkeypatch.setattr(observers, "collect_attention_facts", no_observer_change)
    mock = mock_llm_factory([make_response("A fresh thought")])
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    set_schedule_due("free_time", now)
    await heartbeat.run_main_runtime_tick(1, now=now)
    now += timedelta(minutes=2)
    await heartbeat.run_main_runtime_tick(1, now=now)
    assert len(delivered) == 2
    assert len(mock.call_log) == 1
    assert [row["content"] for row in get_recent_messages(1)] == ["A fresh thought"]
