from datetime import datetime, timedelta, timezone

import pytest

from mochi import db
from mochi.dream import create_dream_session
from mochi.dream_store import complete_batch, inspect_pressure, load_batch, prepare_batch


@pytest.mark.asyncio
async def test_dream_resumes_without_skipping_material_or_repeating_actions():
    now = datetime.now(timezone.utc)
    ids = {db.insert_memory_item(1, f"Fact {index}", 1, source="test") for index in range(85)}
    with db._connect() as conn:
        conn.execute("UPDATE memory_items SET created_at=?,updated_at=?", (now.isoformat(), now.isoformat()))
    batch = prepare_batch(1, now)
    options = dict(user_id=1, channel_id=1, transport="fake", logical_date=now.date().isoformat(), period_key=batch)
    args = {
        "action": "create", "intent": "Revisit this later.",
        "remind_at": (now + timedelta(hours=1)).isoformat(),
    }
    first = await create_dream_session(**options).execute("manage_dream_self_reminder", args)
    assert first.success and first.state_changed
    assert prepare_batch(1, now) == batch
    repeated = await create_dream_session(**options).execute("manage_dream_self_reminder", args)
    assert repeated.success and not repeated.state_changed
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
    reviewed = set()
    for day, size in enumerate((40, 40, 5)):
        moment = now + timedelta(days=day)
        batch = prepare_batch(1, moment)
        shown = {item["id"] for item in load_batch(1, batch)["material"]["memory"]}
        assert len(shown) == size and shown.isdisjoint(reviewed)
        assert inspect_pressure(1, moment)["total"] == len(ids - reviewed)
        complete_batch(1, batch, moment)
        reviewed |= shown
    assert reviewed == ids
    assert not inspect_pressure(1, now + timedelta(days=3))["eligible"]
