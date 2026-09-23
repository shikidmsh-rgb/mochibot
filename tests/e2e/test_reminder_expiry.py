"""Reminder freshness across preparation, retries, restart, and transport chunks."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mochi.ai_client import ChatResult
from mochi.db import _connect, get_recent_messages
from mochi.main_runtime import DurableChatResult
import mochi.reminder_timer as timer
import mochi.skills.reminder.queries as queries


@pytest.fixture
def clock(monkeypatch):
    clock = {"now": datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    return clock


def _create(kind, due):
    if kind == "self":
        return queries.create_self_reminder(1, 100, "check in", due, "fake")
    return queries.create_reminder(1, 100, "Time for bed.", due)


def _row(rid):
    conn = _connect()
    try:
        return dict(conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone())
    finally:
        conn.close()


def _update(rid, **values):
    conn = _connect()
    try:
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn.execute(
            f"UPDATE reminders SET {assignments} WHERE id = ?",
            (*values.values(), rid),
        )
        conn.commit()
    finally:
        conn.close()


def _wire(monkeypatch, kind, *, prepare=None, deliver=None):
    if prepare is None:
        prepare = (
            AsyncMock(return_value=ChatResult(text="Time for bed."))
            if kind == "self" else Mock(wraps=timer._notification_text)
        )
    if deliver is None:
        deliver = AsyncMock(return_value=True)
    if kind == "self":
        timer.set_self_reminder_callbacks(prepare, deliver, "fake")
    else:
        monkeypatch.setattr(timer, "_notification_text", prepare)
        timer.set_send_callback(deliver)
    return prepare, deliver


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
@pytest.mark.parametrize("status", ["pending", "running", "ready"])
async def test_restart_expires_old_work_without_preparing_or_sending(
    monkeypatch, clock, kind, status,
):
    rid = _create(kind, (clock["now"] - timedelta(days=2)).isoformat())
    prepared = DurableChatResult(text="It is 11pm.").to_json()
    _update(
        rid, status=status, attempt_count=44, prepared_text="It is 11pm.",
        result_json=prepared, claimed_at=clock["now"].isoformat(),
        lease_until=(clock["now"] + timedelta(minutes=5)).isoformat(),
        next_attempt_at=(clock["now"] + timedelta(hours=1)).isoformat(),
    )
    prepare, deliver = _wire(monkeypatch, kind)
    stale_heap_entry = _row(rid)
    timer._reload_heap()
    assert timer._heap == []
    await timer._fire_reminder(stale_heap_entry)
    row = _row(rid)
    assert row["status"] == row["outcome"] == "expired"
    assert row["fired"] == 1
    assert row["delivered_at"] is None
    assert row["handled_at"] == clock["now"].isoformat()
    assert row["next_attempt_at"] is row["lease_until"] is None
    assert row["attempt_count"] == 44
    assert row["result_json"] == prepared
    assert row["prepared_text"] == "It is 11pm."
    prepare.assert_not_called()
    deliver.assert_not_awaited()
    assert get_recent_messages(1) == []


@pytest.mark.parametrize("seconds,expired", [(299.999, False), (300, True), (301, True)])
def test_exact_five_minute_boundary(clock, seconds, expired):
    due = (clock["now"] - timedelta(seconds=seconds)).isoformat()
    rid = _create("notify", due)
    claim = queries.claim_reminder(rid, now=clock["now"])
    assert (claim is None) is expired
    assert (_row(rid)["status"] == "expired") is expired


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
async def test_retry_wakes_only_to_expire_at_original_deadline(
    monkeypatch, clock, kind,
):
    rid = _create(kind, (clock["now"] - timedelta(seconds=270)).isoformat())
    prepare, deliver = _wire(
        monkeypatch, kind, deliver=AsyncMock(return_value=False),
    )
    await timer._fire_reminder(_row(rid))
    row = _row(rid)
    deadline = clock["now"] + timedelta(seconds=30)
    assert row["next_attempt_at"] == deadline.isoformat()
    assert row["attempt_count"] == 1
    clock["now"] = deadline
    await timer._fire_reminder(row)
    assert _row(rid)["status"] == "expired"
    assert prepare.call_count == deliver.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
async def test_preparation_crossing_deadline_never_sends(
    monkeypatch, clock, kind,
):
    rid = _create(kind, (clock["now"] - timedelta(seconds=299)).isoformat())

    def slow_prepare(*args, **kwargs):
        clock["now"] += timedelta(seconds=2)
        return ChatResult(text="Too late.") if kind == "self" else f"⏰ {args[0]}"

    prepare, deliver = _wire(
        monkeypatch, kind,
        prepare=(AsyncMock if kind == "self" else Mock)(side_effect=slow_prepare),
    )
    await timer._fire_reminder(_row(rid))
    assert prepare.call_count == 1
    deliver.assert_not_awaited()
    assert _row(rid)["status"] == "expired"
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
@pytest.mark.parametrize("transport_name", ["wechat", "telegram"])
async def test_expiry_between_chunks_stops_remaining_sends(
    monkeypatch, clock, kind, transport_name,
):
    import mochi.transport.weixin as weixin
    import mochi.transport.telegram as telegram

    rid = _create(kind, (clock["now"] - timedelta(seconds=299)).isoformat())
    text = "First bubble here.|||Second bubble here."
    _update(rid, message=text)

    async def first_chunk(*args, **kwargs):
        clock["now"] += timedelta(seconds=2)
        return {}

    sender = AsyncMock(side_effect=first_chunk)
    if transport_name == "wechat":
        transport = weixin.WeixinTransport()
        transport.restore_owner_id("owner")
        transport._session = object()
        transport._remember_context_token("owner", "test-context")
        monkeypatch.setattr(weixin, "WEIXIN_BUBBLE_DELAY_S", 0)
        monkeypatch.setattr(weixin, "WEIXIN_MSG_LIMIT", 20)
        monkeypatch.setattr(transport, "_api_post", sender)
    else:
        transport = telegram.TelegramTransport()
        transport._app = SimpleNamespace(bot=SimpleNamespace(
            send_message=sender, send_chat_action=AsyncMock(),
        ))
        monkeypatch.setattr(telegram, "TG_BUBBLE_DELAY_S", 0)

    async def deliver(user_id, result, *, can_deliver):
        if isinstance(result, str):
            result = ChatResult(text=result)
        return await transport.send_proactive_result_checked(
            user_id, result, can_deliver=can_deliver,
        )

    _wire(
        monkeypatch, kind,
        prepare=AsyncMock(return_value=ChatResult(text=text)) if kind == "self" else None,
        deliver=deliver,
    )
    await timer._fire_reminder(_row(rid))
    assert sender.await_count == 1
    assert _row(rid)["status"] == "expired"
    assert get_recent_messages(1) == []


def test_recurring_expiry_preserves_evidence_and_skips_missed_dates(clock):
    rid = _create("notify", "2026-09-12T22:00:00+08:00")
    _update(rid, recurrence="daily", status="ready", prepared_text="Old bedtime.")
    scheduled = queries.get_schedulable_reminders(now=clock["now"])
    assert len(scheduled) == 1
    assert scheduled[0]["id"] != rid
    assert scheduled[0]["remind_at"] == "2026-09-15T22:00:00+08:00"
    assert scheduled[0]["prepared_text"] is None
    assert _row(rid)["prepared_text"] == "Old bedtime."
    assert _row(rid)["status"] == "expired"
    assert queries.get_schedulable_reminders(now=clock["now"]) == scheduled


def test_abandoned_claim_wakes_at_deadline_not_later_lease(clock):
    rid = _create("notify", (clock["now"] - timedelta(minutes=4)).isoformat())
    assert queries.claim_reminder(rid, now=clock["now"]) is not None
    assert queries.get_next_active_lease_expiry(now=clock["now"]) == (
        clock["now"] + timedelta(minutes=1)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recurrence", [None, "daily"])
async def test_notify_retry_inside_window_reuses_prepared_text(monkeypatch, clock, recurrence):
    import mochi.llm as llm
    model = Mock(side_effect=AssertionError("notify must not request a model"))
    monkeypatch.setattr(llm, "get_client_for_tier", model)
    rid = _create("notify", clock["now"].isoformat())
    _update(rid, recurrence=recurrence)
    next_due = clock["now"] + timedelta(days=1)
    prepare, deliver = _wire(
        monkeypatch, "notify", deliver=AsyncMock(side_effect=[False, True]),
    )
    await timer._fire_reminder(_row(rid))
    clock["now"] += timedelta(seconds=61)
    await timer._fire_reminder(_row(rid))
    assert _row(rid)["status"] == ("pending" if recurrence else "delivered")
    if recurrence:
        assert _row(rid)["remind_at"] == next_due.isoformat()
        assert _row(rid)["prepared_text"] is None
    assert prepare.call_count == 1
    assert deliver.await_count == 2
    assert all(call.args[1] == "⏰ Time for bed." for call in deliver.call_args_list)
    assert [m["content"] for m in get_recent_messages(1)] == ["⏰ Time for bed."]
    model.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,sweep", [("skip", True), ("handled", False)])
async def test_recurring_silent_result_after_deadline_preserves_one_successor(
    monkeypatch, clock, outcome, sweep,
):
    rid = _create("self", clock["now"].isoformat())
    _update(rid, recurrence="daily")

    async def prepare(entry):
        clock["now"] += timedelta(seconds=301)
        if sweep:
            queries.expire_overdue_reminders(now=clock["now"])
        return ChatResult(disposition=outcome, successful_effects=(outcome == "handled"))

    _, deliver = _wire(monkeypatch, "self", prepare=prepare)
    await timer._fire_reminder(_row(rid))
    row = _row(rid)
    assert row["status"] == row["outcome"] == "expired"
    assert row["result_json"] and row["delivered_at"] is None
    pending = queries.get_schedulable_reminders(now=clock["now"])
    assert len(pending) == 1 and pending[0]["id"] != rid
    assert pending[0]["result_json"] is None
    assert queries.get_schedulable_reminders(now=clock["now"]) == pending
    deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
@pytest.mark.parametrize("late", [False, True])
async def test_creation_exposes_deadline_and_rejects_expired_time(
    monkeypatch, clock, kind, late,
):
    import mochi.skills.reminder.handler as handler
    from mochi.skills import get_skill
    from mochi.skills.base import SkillContext

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz)

    monkeypatch.setattr(handler, "datetime", Clock)
    due = clock["now"] + timedelta(minutes=-5 if late else 10)
    result = await get_skill("reminder").execute(SkillContext(
        trigger="tool_call", user_id=1, channel_id=100, transport="fake", actor="main",
        args={
            "action": "create", "kind": kind, "remind_at": due.isoformat(),
            "intent" if kind == "self" else "message": "Check in.",
        },
    ))
    assert result.success is not late
    active = queries.get_active_reminders(1)
    if late:
        assert active == []
        assert "five minutes past" in result.output
    else:
        assert len(active) == 1
        assert (due + timedelta(minutes=5)).isoformat() in result.output


@pytest.mark.asyncio
async def test_self_model_timeout_uses_remaining_window(monkeypatch, clock):
    import asyncio

    rid = _create("self", (clock["now"] - timedelta(seconds=299)).isoformat())
    prepare, deliver = _wire(monkeypatch, "self")

    async def timeout_at_deadline(awaitable, *, timeout):
        assert timeout == 1
        awaitable.close()
        clock["now"] += timedelta(seconds=1)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(timer.asyncio, "wait_for", timeout_at_deadline)
    await timer._fire_reminder(_row(rid))
    assert _row(rid)["status"] == "expired"
    prepare.assert_not_awaited()
    deliver.assert_not_awaited()


@pytest.mark.parametrize("invalid", ["time", "recurrence"])
def test_invalid_record_does_not_block_valid_reminders(clock, invalid, caplog):
    broken = _create("notify", (clock["now"] - timedelta(days=1)).isoformat())
    if invalid == "time":
        _update(broken, remind_at="not-a-time")
    else:
        _update(broken, recurrence="monthly_on:abc")
    valid = _create("notify", clock["now"].isoformat())
    scheduled = queries.get_schedulable_reminders(now=clock["now"])
    assert [row["id"] for row in scheduled] == [valid]
    assert _row(broken)["outcome"] == "invalid_schedule"
    assert "invalid schedule" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
async def test_sweep_during_preparation_retains_result_without_sending(
    monkeypatch, clock, kind,
):
    rid = _create(kind, (clock["now"] - timedelta(seconds=299)).isoformat())

    def prepare(*args, **kwargs):
        clock["now"] += timedelta(seconds=2)
        queries.expire_overdue_reminders(now=clock["now"])
        return ChatResult(text="Prepared.") if kind == "self" else f"⏰ {args[0]}"

    _, deliver = _wire(
        monkeypatch, kind, prepare=(AsyncMock if kind == "self" else Mock)(side_effect=prepare),
    )
    await timer._fire_reminder(_row(rid))
    row = _row(rid)
    assert row["status"] == "expired"
    if kind == "self":
        assert DurableChatResult.from_json(row["result_json"]).text == "Prepared."
    else:
        assert row["prepared_text"] == "⏰ Time for bed."
    deliver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["notify", "self"])
@pytest.mark.parametrize("recurring", [False, True])
async def test_started_send_can_record_success_after_expiry_sweep(
    monkeypatch, clock, kind, recurring,
):
    rid = _create(kind, (clock["now"] - timedelta(seconds=299)).isoformat())
    if recurring:
        _update(rid, recurrence="daily")
    text = "A send already in flight."
    _update(rid, message=text)
    result = ChatResult(text=text, _pending_history={
        "user_id": 1, "content": text, "turn_id": "in-flight-reminder",
        "tool_history": None, "processed": True,
    })

    async def deliver(*args, can_deliver):
        assert can_deliver()
        clock["now"] += timedelta(seconds=2)
        queries.expire_overdue_reminders(now=clock["now"])
        assert not can_deliver()
        return True

    _wire(
        monkeypatch, kind, deliver=deliver,
        prepare=AsyncMock(return_value=result) if kind == "self" else None,
    )
    await timer._fire_reminder(_row(rid))
    row = _row(rid)
    assert row["status"] == row["outcome"] == "delivered"
    assert row["delivered_at"] == clock["now"].isoformat()
    assert get_recent_messages(1)[0]["content"] == (text if kind == "self" else f"⏰ {text}")
    assert len(queries.get_schedulable_reminders(now=clock["now"])) == int(recurring)
