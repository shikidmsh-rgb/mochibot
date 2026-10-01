"""Essential autonomous Main behavior."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
import random

import pytest

from mochi.ai_client import ChatResult, chat
from mochi.core_store import replace_core
from mochi.db import _connect, get_conversation_context, get_recent_messages, save_message
from mochi.heartbeat_runtime import ensure_daily_free_time_plan
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry
from mochi.transport import DeliveryError, IncomingMessage
from tests.e2e.mock_llm import main_context, make_response, make_tool_call


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_bedtime_delivery_outlives_preparation_deadline(monkeypatch, confirmed):
    import mochi.heartbeat as heartbeat

    monkeypatch.setattr(heartbeat, "bedtime_entry_timeout", lambda: 0.01)
    prepared = []
    deliveries = []

    async def prepare(entry):
        prepared.append(entry)
        return ChatResult(text="Good night.", _pending_history={
            "user_id": 1, "content": "Good night.", "turn_id": "bedtime:deadline",
            "processed": True, "tool_history": None,
        })

    async def deliver(_channel_id, _result, *, can_deliver):
        deliveries.append(True)
        assert can_deliver()
        assert get_recent_messages(1) == []
        await asyncio.sleep(0.03)
        assert can_deliver()
        if not confirmed:
            raise DeliveryError("receipt timed out", outcome="delivery_unknown")
        return True

    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    assert await heartbeat.run_silent_bedtime(1, "silence") is confirmed
    assert heartbeat._state == heartbeat.SLEEPING
    assert len(prepared) == len(deliveries) == 1
    assert prepared[0].kind == "bedtime"
    assert prepared[0].trigger == "silence"
    messages = get_recent_messages(1)
    assert len(messages) == int(confirmed)
    if confirmed:
        assert messages[0]["turn_id"] == "bedtime:deadline"
        assert messages[0]["role"] == "assistant"
    conn = _connect()
    action = conn.execute(
        "SELECT action FROM heartbeat_log ORDER BY id DESC LIMIT 1",
    ).fetchone()[0]
    conn.close()
    assert action == ("bedtime_entry" if confirmed else "bedtime_delivery_unknown")
    assert not await heartbeat.run_silent_bedtime(1, "silence")
    assert len(prepared) == len(deliveries) == 1


@pytest.mark.asyncio
async def test_bedtime_preparation_still_times_out_without_sending(monkeypatch, caplog):
    import mochi.heartbeat as heartbeat

    monkeypatch.setattr(heartbeat, "bedtime_entry_timeout", lambda: 0.01)
    cancelled = []

    async def prepare(_entry):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    async def deliver(*_args, **_kwargs):
        pytest.fail("timed-out preparation must not deliver")

    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    assert not await heartbeat.run_silent_bedtime(1, "silence")
    assert cancelled == [True]
    assert heartbeat._state == heartbeat.SLEEPING
    assert get_recent_messages(1) == []
    conn = _connect()
    record = conn.execute(
        "SELECT action, summary FROM heartbeat_log ORDER BY id DESC LIMIT 1",
    ).fetchone()
    conn.close()
    assert record["action"] == "bedtime_timeout"
    assert "preparation_timeout_s=0.01" in record["summary"]
    assert "Bedtime preparation timed out" in caplog.text


def _schedule_due(now):
    from mochi.admin.admin_db import set_system_override

    set_system_override("MAX_DAILY_PROACTIVE", "1")
    keys = ensure_daily_free_time_plan(
        user_id=1, channel_id=1, transport="fake",
        now=now.replace(hour=6, minute=0, second=0, microsecond=0),
        max_daily=1, awake=True, rng=random.Random(1),
    )
    assert len(keys) == 1
    conn = _connect()
    conn.execute(
        "UPDATE heartbeat_runs SET next_attempt_at = ? WHERE run_key = ?",
        (now.isoformat(), keys[0]),
    )
    conn.commit()
    conn.close()
    return keys[0]


def test_observer_rediscovery_preserves_runtime_cache():
    import mochi.observers as observers

    observers.discover()
    original = observers.get_observer("time_context")
    original._last_data = {"date": "cache-marker"}
    original._last_collected_at = datetime(
        2026, 8, 15, 12, 0, tzinfo=timezone.utc,
    )

    observers.discover()
    assert observers.get_observer("time_context") is original
    assert original._last_data == {"date": "cache-marker"}


@pytest.mark.asyncio
async def test_free_time_carries_completed_history_without_reopening_old_turns(
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
    reasoning_source = "https://api.deepseek.com/v1::model"
    for number in range(3):
        turn_id = f"free-time-history-{number}"
        save_message(1, "user", f"user-{number}", turn_id=turn_id)
        save_message(
            1, "assistant", f"assistant-{number}", turn_id=turn_id,
            reasoning_content="OLD_TURN_REASONING", reasoning_source=reasoning_source,
        )
    save_message(
        1, "assistant", "Already delivered.\nDo not lose this context.",
        turn_id="free_time:delivered", processed=True,
        reasoning_content="OLD_WAKE_REASONING", reasoning_source=reasoning_source,
    )
    save_message(1, "user", "unpaired-user-message", turn_id="incomplete-turn")
    before = get_recent_messages(1)
    expected = get_conversation_context(1, 2, include_summary=False)["recent"]
    tool_response = make_response(tool_calls=[
        make_tool_call("write_diary", {"content": "A new observation."}),
    ])
    tool_response.reasoning_content = "CURRENT_TOOL_REASONING"
    mock = mock_llm_factory([tool_response, make_response("[SKIP]")])
    monkeypatch.setattr(
        type(mock), "reasoning_source", property(lambda self: reasoning_source),
    )
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
    assert result.disposition == "handled"
    assert "CORE_MARKER" in prompt
    initial = mock.call_log[0]["messages"]
    assert [item["role"] for item in initial] == ["system", "user"]
    records = json.loads(initial[-1]["content"].split(
        '<recent_completed_turns role="read_only_evidence">\n', 1,
    )[1].split("\n</recent_completed_turns>", 1)[0])
    assert records == [
        {
            "speaker": item["role"], "timestamp": item["created_at"],
            "content": item["content"],
        }
        for item in expected
    ]
    assert [item["content"] for item in records] == [
        "user-1", "assistant-1", "user-2", "assistant-2",
        "Already delivered.\nDo not lose this context.",
    ]
    replay = json.dumps(mock.call_log, ensure_ascii=False)
    assert "OLD_TURN_REASONING" not in replay
    assert "OLD_WAKE_REASONING" not in replay
    continuation = next(
        message for message in mock.call_log[1]["messages"]
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert continuation["reasoning_content"] == "CURRENT_TOOL_REASONING"
    assert get_recent_messages(1) == before


@pytest.mark.asyncio
async def test_free_time_diary_revisions_preserve_visible_snapshot_boundary(
    mock_llm_factory,
):
    from mochi.db import get_tool_executions_for_turn
    from mochi.diary import diary

    source_date, _, _ = diary.read_write_snapshot()
    original = "Existing journal."
    diary.replace_section_exact(
        "今日日記", expected_content="", content=original, target_date=source_date,
    )
    intermediate = original + "\nFirst observation."
    revised = intermediate + "\nNew observation."
    mock = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("write_diary", {"content": intermediate}),
            make_tool_call("write_diary", {"content": "A stale revision in the same round"}),
        ]),
        make_response(tool_calls=[
            make_tool_call("write_diary", {"content": revised}),
        ]),
        make_response("[SKIP]"),
    ])
    entry = MainRuntimeEntry.free_time(
        run_key="free_time:diary-retry", wake_reason="periodic",
        user_id=1, channel_id=100, transport="fake", claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    )

    result = await chat(runtime_entry=entry)

    assert original in main_context(mock.call_log[0]["messages"])
    snapshots = [
        message["content"] for message in mock.call_log[1]["messages"]
        if message["role"] == "tool"
    ]
    assert len(snapshots) == 2
    assert intermediate in json.loads(snapshots[1])["message"]
    executions = get_tool_executions_for_turn(entry.run_key)
    assert [(row["status"], bool(row["state_changed"])) for row in executions] == [
        ("success", True), ("failed", False), ("success", True),
    ]
    assert diary.read("今日日記") == revised
    from mochi.runtime_trace import get_run
    diagnostics = get_run(1, result._trace_id)["diagnostics"]
    assert diagnostics["state"] == "recovered"
    assert diagnostics["issues"][0]["ref"] == f"tool:{executions[1]['id']}"
    assert diagnostics["issues"][0]["recovered_by"] == f"tool:{executions[2]['id']}"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_free_time_sees_current_habits_without_loading_write_tools(
    mock_llm_factory, enabled,
):
    from mochi.db import set_skill_enabled
    from mochi.diary import diary
    from mochi.skills.habit.handler import _current_period
    from mochi.skills.habit.queries import (
        add_habit, get_habit_checkins, mutate_habit, record_habit_progress,
    )

    pending = add_habit(0, "Supplement", "daily:1", importance="important")
    complete = add_habit(0, "Water", "daily:6")
    period = _current_period("daily")
    record_habit_progress(complete, 0, period, total=6)
    paused = add_habit(0, "Paused walk", "daily:1")
    mutate_habit(0, paused, paused_until="2099-01-01")
    add_habit(2, "Another user's habit", "daily:1")
    day, _, _ = diary.read_write_snapshot()
    diary.replace_section_exact(
        "今日日記", expected_content="", content="Today journal context", target_date=day,
    )
    set_skill_enabled("habit", enabled)
    mock = mock_llm_factory([make_response("[SKIP]")])

    await chat(runtime_entry=MainRuntimeEntry.free_time(
        run_key="free_time:habit-context", wake_reason="periodic",
        user_id=0, channel_id=0, transport="fake", claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    ))

    context = main_context(mock.call_log[0]["messages"])
    assert "Today journal context" in context
    if enabled:
        pending_line = next(line for line in context.splitlines() if "Supplement" in line)
        complete_line = next(line for line in context.splitlines() if "Water" in line)
        assert f"[habit_id={pending}]" in pending_line and "0/1" in pending_line
        assert "⚡" in pending_line
        assert f"[habit_id={complete}]" in complete_line and "6/6" in complete_line
    else:
        assert "Supplement" not in context and "Water" not in context
    assert "Paused walk" not in context
    assert "Another user's habit" not in context
    tool_names = {tool["function"]["name"] for tool in mock.call_log[0]["tools"]}
    assert not tool_names & {"habit_progress", "edit_habit"}
    assert get_habit_checkins(pending, period) == []
    assert len(get_habit_checkins(complete, period)) == 6


@pytest.mark.asyncio
async def test_runtime_trace_links_model_tools_and_delivery_without_becoming_context(
    mock_llm_factory, monkeypatch,
):
    from mochi import runtime_trace as trace
    from mochi.diary import diary

    with trace.run_scope("diagnostic-only", "provider", 1, {
        "evidence": "ADMIN_ONLY_EVIDENCE",
    }) as previous:
        trace.finish_run(previous.trace_id, "completed")

    mock = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("write_diary", {"content": "Persisted by this turn."}),
        ]),
        make_response("A deliverable response."),
    ])
    original = mock.chat

    def recorded(**kwargs):
        return trace.sdk_call(original, protocol="test", provider="test", **kwargs)

    monkeypatch.setattr(mock, "chat", recorded)
    entry = MainRuntimeEntry.free_time(
        run_key="free_time:traced", wake_reason="periodic", user_id=1,
        channel_id=1, transport="fake", claim_token="claim",
        lease_until="2099-01-01T00:00:00+00:00",
    )
    result = await chat(runtime_entry=entry)
    evidence = trace.get_run(1, result._trace_id)
    assert evidence["turn_id"] == entry.run_key
    assert evidence["spans"][0]["status"] == "prepared"
    assert len([item for item in evidence["spans"] if item["span_kind"] == "model"]) == 2
    assert [item["tool_name"] for item in evidence["tools"]] == ["write_diary"]
    assert evidence["tools"][0]["state_changed"] == 1
    assert diary.read("今日日記") == "Persisted by this turn."
    assert "ADMIN_ONLY_EVIDENCE" not in json.dumps(mock.call_log)
    assert get_recent_messages(1) == []

    restored = ChatResult.from_durable(result.to_durable())
    assert restored._trace_id == result._trace_id
    assert restored.confirm_delivered(final=True)
    evidence = trace.get_run(1, result._trace_id)
    assert evidence["spans"][0]["status"] == "delivered"
    assert any(item["span_kind"] == "delivery" for item in evidence["spans"])


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", ["chat", "free_time"])
async def test_day_start_look_rides_only_the_first_owner_turn(
    mock_llm_factory, monkeypatch, carrier,
):
    from datetime import date

    import mochi.heartbeat as heartbeat
    from mochi.config import logical_today
    from mochi.diary import diary

    monkeypatch.setattr(heartbeat, "_is_awake_hour", lambda hour: True)
    yesterday = date.fromisoformat(logical_today()) - timedelta(days=1)
    archive = diary.path.parent / "diary_archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{yesterday:%Y-%m}.md").write_text(
        f"# Diary {yesterday}\n\n## 今日日記\nYESTERDAY_MARKER\n",
        encoding="utf-8",
    )
    mock = mock_llm_factory([make_response(text) for text in (
        "stranger", "first", "second",
    )])

    def owner(text, *, owner_authorized=True):
        return IncomingMessage(
            user_id=1, channel_id=100, text=text, transport="fake",
            owner_authorized=owner_authorized,
        )

    (await chat(owner("hi", owner_authorized=False))).confirm_delivered(final=True)
    if carrier == "chat":
        (await chat(owner("morning"))).confirm_delivered(final=True)
    else:
        await chat(runtime_entry=MainRuntimeEntry.free_time(
            run_key="free_time:day-start", wake_reason="periodic", user_id=1,
            channel_id=100, transport="fake", claim_token="claim",
            lease_until="2099-01-01T00:00:00+00:00",
        ))
    await chat(owner("again"))

    stranger, first, second = (
        main_context(call["messages"]) for call in mock.call_log
    )
    assert "YESTERDAY_MARKER" not in stranger
    assert "<recent_diary>" in first and "YESTERDAY_MARKER" in first
    assert "YESTERDAY_MARKER" not in second


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "free_time", "self_reminder", "bedtime"])
async def test_main_sees_delivered_autonomous_history_only(
    mock_llm_factory, kind,
):
    delivered = ChatResult(_pending_history={
        "user_id": 1, "content": "ALREADY_SAID_GOODNIGHT",
        "turn_id": "free_time:delivered", "processed": True, "tool_history": None,
    })
    assert delivered.confirm_delivered()
    assert not delivered.confirm_delivered()
    ChatResult(_pending_history={
        "user_id": 1, "content": "UNSENT_DRAFT",
        "turn_id": "free_time:failed", "processed": True, "tool_history": None,
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
    if kind == "bedtime":
        line = next(
            line for line in delivered_messages[0]["content"].splitlines()
            if line.startswith('{"date":')
        )
        review = json.loads(line)
        assert review["messages"][0]["role"] == "assistant"
        assert review["messages"][0]["content"] == "ALREADY_SAID_GOODNIGHT"
    elif kind == "free_time":
        assert delivered_messages[0]["role"] == "user"
        records = json.loads(delivered_messages[0]["content"].split(
            '<recent_completed_turns role="read_only_evidence">\n', 1,
        )[1].split("\n</recent_completed_turns>", 1)[0])
        assert len(records) == 1
        assert records[0]["speaker"] == "assistant"
        assert records[0]["content"] == "ALREADY_SAID_GOODNIGHT"
    else:
        assert delivered_messages[0]["role"] == "assistant"
        assert delivered_messages[0]["content"] == "ALREADY_SAID_GOODNIGHT"
        history_index = messages.index(delivered_messages[0])
        assert f"{history_index}. assistant: " in main_context(messages)
    assert not any(
        value in message.get("content", "")
        for message in messages for value in ("UNSENT_DRAFT", "OTHER_USER")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["explicit", "silence"])
async def test_bedtime_reviews_the_day_and_keeps_silent_write_receipts(
    mock_llm_factory, trigger,
):
    import mochi.heartbeat as heartbeat
    from mochi.diary import diary

    for number in range(12):
        save_message(1, "user", f"user-{number}", turn_id=f"day-{number}")
        save_message(1, "assistant", f"reply-{number}", turn_id=f"day-{number}")
    responses = []
    if trigger == "explicit":
        responses.append(make_response(tool_calls=[make_tool_call("enter_bedtime", {})]))
    responses.extend([
        make_response(tool_calls=[
            make_tool_call("write_diary", {"content": "Remembered the day."}),
        ]),
        make_response("[SKIP]"),
    ])
    mock = mock_llm_factory(responses)
    prepared = []

    async def prepare(entry):
        result = await chat(runtime_entry=entry)
        prepared.append(result)
        return result

    async def deliver(*args, **kwargs):
        pytest.fail("silent bedtime must not attempt delivery")

    if trigger == "explicit":
        result = await chat(IncomingMessage(
            user_id=1, channel_id=1, transport="fake", text="Good night.",
        ))
        assert result.bedtime_requested
        receipt = next(
            item for item in mock.call_log[1]["messages"]
            if item["role"] == "tool"
        )
        context = json.loads(receipt["content"])["bedtime_context"]
    else:
        heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
        assert await heartbeat.run_silent_bedtime(1, "silence")
        assert heartbeat._state == heartbeat.SLEEPING
        result = prepared[0]
        context = mock.call_log[0]["messages"][0]["content"]
    review = json.loads(next(
        line for line in context.splitlines() if line.startswith('{"date":')
    ))
    assert review["total_messages"] == review["shown_messages"] == (
        25 if trigger == "explicit" else 24
    )
    assert not review["truncated"]
    assert review["messages"][0]["content"] == "user-0"
    assert diary.read(section="今日日記") == "Remembered the day."
    assert result.disposition == "handled"
    assert result.successful_effects
    assert result.tool_audit == [
        {"name": "write_diary", "status": "success", "state_changed": True},
    ]
    assert result.text == ""
    assert result._pending_history is None
    assert len(mock.call_log) == len(responses)


@pytest.mark.asyncio
async def test_sleeping_tick_does_not_create_a_free_time_plan(monkeypatch):
    import mochi.heartbeat as heartbeat
    import mochi.observers as observers

    async def unexpected():
        pytest.fail("sleeping tick must not collect or enter Main")

    monkeypatch.setattr(heartbeat, "_state", heartbeat.SLEEPING)
    monkeypatch.setattr(observers, "collect_all", unexpected)
    assert await heartbeat.run_main_runtime_tick(
        1, now=datetime(2026, 9, 25, 8, tzinfo=timezone.utc),
    ) == []
    conn = _connect()
    assert conn.execute("SELECT COUNT(*) FROM heartbeat_schedules").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM heartbeat_runs").fetchone()[0] == 0
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_kind", ["free_time", "chat"])
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

    prompt = main_context(mock.call_log[3]["messages"])
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

    monkeypatch.setattr(observers, "collect_all", no_observer_change)
    mock = mock_llm_factory([make_response("I was thinking of you.")])
    deliveries = 0
    prepared = 0

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

    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    run_key = _schedule_due(clock["now"])
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    assert get_recent_messages(1) == []

    clock["now"] += timedelta(seconds=61)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])
    clock["now"] += timedelta(days=1)
    await heartbeat.run_main_runtime_tick(1, now=clock["now"])

    assert prepared == 1
    assert deliveries == 1
    assert len(mock.call_log) == 1
    assert get_recent_messages(1) == []
    conn = _connect()
    row = conn.execute(
        "SELECT status, outcome, attempt_count, next_attempt_at, last_error "
        "FROM heartbeat_runs WHERE run_key = ?", (run_key,),
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
async def test_prepared_outbox_is_never_replayed(monkeypatch):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    clock = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: clock)

    async def no_observer_change():
        return False

    async def unexpected(*_args, **_kwargs):
        pytest.fail("an abandoned outbox must never re-enter Main or delivery")

    monkeypatch.setattr(observers, "collect_all", no_observer_change)
    heartbeat.set_main_runtime_callbacks(unexpected, unexpected, "fake")

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
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
async def test_disable_switch_skips_due_turn_without_replay(monkeypatch):
    import mochi.config as cfg
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)

    async def unexpected(*args, **kwargs):
        pytest.fail("disabled or missed Free Time must not run")

    collections = []

    async def collect():
        collections.append(True)
        return {}

    monkeypatch.setattr(observers, "collect_all", collect)
    heartbeat.set_main_runtime_callbacks(unexpected, unexpected, "fake")
    run_key = _schedule_due(now)
    monkeypatch.setattr(cfg, "FREE_TIME_ENABLED", False)
    assert await heartbeat.run_main_runtime_tick(1, now=now) == []
    assert len(collections) == 1
    conn = _connect()
    row = conn.execute(
        "SELECT status, attempt_count, outcome FROM heartbeat_runs WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    conn.close()
    assert tuple(row) == ("expired", 0, "expired")

    monkeypatch.setattr(cfg, "FREE_TIME_ENABLED", True)

    await heartbeat.run_main_runtime_tick(1, now=now + timedelta(seconds=30))
    assert len(collections) == 2
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", [
    "sleep", "sleep_wake", "lease", "chat_finished", "chat_effects",
])
async def test_late_main_result_is_not_delivered(monkeypatch, interruption):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)

    async def no_observer_change():
        return False

    async def prepare(_entry):
        nonlocal now
        if interruption == "lease":
            now += timedelta(seconds=301)
        elif interruption in {"chat_finished", "chat_effects"}:
            with heartbeat.active_chat():
                assert heartbeat.has_active_chat()
            assert not heartbeat.has_active_chat()
            effects = interruption == "chat_effects"
            return ChatResult(
                disposition="handled" if effects else "skip",
                tool_audit=[{"name": "completed", "state_changed": effects}],
                successful_effects=effects,
            )
        else:
            monkeypatch.setattr(heartbeat, "_state_changed_at", now)
            if interruption == "sleep":
                monkeypatch.setattr(heartbeat, "_state", heartbeat.SLEEPING)
        return ChatResult(text="An old thought")

    async def deliver(*args, **kwargs):
        pytest.fail("late Main result must not be delivered")

    monkeypatch.setattr(observers, "collect_all", no_observer_change)
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    run_key = _schedule_due(now)
    await heartbeat.run_main_runtime_tick(1, now=now)
    conn = _connect()
    row = conn.execute(
        "SELECT outcome, next_attempt_at, result_json FROM heartbeat_runs WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    conn.close()
    cancelled_by_chat = interruption in {"chat_finished", "chat_effects"}
    assert row["outcome"] == ("active_chat" if cancelled_by_chat else "expired")
    assert row["next_attempt_at"] is None
    if cancelled_by_chat:
        saved = DurableChatResult.from_json(row["result_json"])
        assert saved.tool_audit == (
            {"name": "completed", "state_changed": interruption == "chat_effects"},
        )
        assert saved.successful_effects == (interruption == "chat_effects")
        assert not saved.text and not saved.stickers and saved.pending_history is None
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["failure", "chat", "lease"])
async def test_delivered_text_survives_sticker_failure(
    monkeypatch, mock_llm_factory, interruption,
):
    import mochi.heartbeat as heartbeat
    import mochi.heartbeat_runtime as runtime
    import mochi.observers as observers

    now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "_utc_now", lambda: now)

    async def no_observer_change():
        return False

    async def prepare(entry):
        result = await chat(runtime_entry=entry)
        result.stickers = ["sticker"]
        return result

    delivered = []

    async def deliver(_channel, result, **_kwargs):
        nonlocal now
        delivered.append(result)
        assert _kwargs["can_deliver"]()
        if result.text and interruption == "chat":
            with heartbeat.active_chat():
                assert not _kwargs["can_deliver"]()
            assert not _kwargs["can_deliver"]()
        if result.text and interruption == "lease":
            now += timedelta(seconds=301)
            assert not _kwargs["can_deliver"]()
        return bool(result.text)

    monkeypatch.setattr(observers, "collect_all", no_observer_change)
    mock = mock_llm_factory([make_response("A fresh thought")])
    heartbeat.set_main_runtime_callbacks(prepare, deliver, "fake")
    run_key = _schedule_due(now)
    await heartbeat.run_main_runtime_tick(1, now=now)
    now += timedelta(minutes=2)
    await heartbeat.run_main_runtime_tick(1, now=now)
    assert len(delivered) == (2 if interruption == "failure" else 1)
    assert len(mock.call_log) == 1
    assert [row["content"] for row in get_recent_messages(1)] == ["A fresh thought"]
    conn = _connect()
    row = conn.execute(
        "SELECT outcome, attempt_count FROM heartbeat_runs WHERE run_key = ?",
        (run_key,),
    ).fetchone()
    conn.close()
    expected_outcome = {
        "failure": "delivery_unknown", "chat": "active_chat", "lease": "expired",
    }[interruption]
    assert tuple(row) == (expected_outcome, 1)
