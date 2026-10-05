from datetime import datetime, timedelta, timezone

import pytest

from mochi import db, reminder_timer as timer
from mochi.ai_client import ChatResult
from mochi.skills.reminder import queries


@pytest.mark.asyncio
async def test_reminder_retry_reuses_output_and_expiry_never_replays(monkeypatch):
    clock = {"now": datetime(2026, 10, 4, 15, tzinfo=timezone.utc)}
    monkeypatch.setattr(timer, "_utc_now", lambda: clock["now"])
    monkeypatch.setattr(queries, "_now", lambda: clock["now"])
    rid = queries.create_self_reminder(1, 1, "Check in", clock["now"].isoformat(), "fake", "daily")
    prepared, deliveries = [], []

    def row():
        with db._connect() as conn:
            return dict(conn.execute("SELECT * FROM reminders WHERE id=?", (rid,)).fetchone())

    async def prepare(entry):
        prepared.append(entry.idempotency_key)
        return ChatResult(text="Prepared once.", _pending_history={
            "user_id": 1, "content": "Prepared once.", "turn_id": entry.idempotency_key,
            "processed": True, "tool_history": None,
        })

    async def deliver(_channel, _result, *, can_deliver):
        assert can_deliver()
        deliveries.append(True)
        return len(deliveries) > 1

    timer.set_self_reminder_callbacks(prepare, deliver, "fake")
    await timer._fire_reminder(row())
    assert db.get_recent_messages(1) == []
    assert row()["result_json"]
    clock["now"] += timedelta(seconds=61)
    await timer._fire_reminder(row())
    assert len(prepared) == 1 and len(deliveries) == 2
    assert len(db.get_recent_messages(1)) == 1
    next_due = datetime.fromisoformat(row()["remind_at"])
    await timer._fire_reminder(row())
    assert len(deliveries) == 2
    clock["now"] = next_due + timedelta(minutes=5)
    await timer._fire_reminder(row())
    assert row()["status"] == "expired"
    assert len(prepared) == 1 and len(deliveries) == 2
    assert len(db.get_recent_messages(1)) == 1
