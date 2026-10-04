"""Incremental maintenance boundaries, without model calls or runtime data."""

from datetime import datetime, timedelta, timezone
import json
import pytest

from mochi.db import _connect, insert_memory_item, update_memory_item
from mochi.dream_store import (
    claim_attempt, complete_batch, inspect_pressure, load_batch, prepare_batch,
)

NOW = datetime(2026, 10, 4, 4, tzinfo=timezone.utc)


def memory(content, when=NOW):
    item_id = insert_memory_item(1, content, 1, source="test")
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET created_at=?, updated_at=? WHERE id=?",
        (when.isoformat(), when.isoformat(), item_id),
    )
    conn.commit()
    conn.close()
    return item_id


def test_context_capacity_matches_visible_core_and_evidence_visibility(monkeypatch):
    from mochi import config
    from mochi.core_store import replace_core
    from mochi.dream import create_dream_session

    visible = "\u7532\u4e59\u4e19\u4e01"
    replace_core("new")
    monkeypatch.setattr(config, "CORE_MAX_TOKENS", 4)
    item_id = memory("A past experience")
    conn = _connect()
    evidence_ids = [
        conn.execute(
            "INSERT INTO messages(user_id,role,content,created_at) VALUES (1,'user',?,?)",
            (f"Source {number}", (NOW - timedelta(days=9)).isoformat()),
        ).lastrowid
        for number in range(3)
    ]
    conn.execute(
        "UPDATE memory_items SET evidence_message_ids=? WHERE id=?",
        (json.dumps(evidence_ids), item_id),
    )
    conn.commit()
    conn.close()
    session = create_dream_session(
        user_id=1, logical_date=NOW.date().isoformat(),
        period_key=prepare_batch(1, NOW), core_content=visible,
    )
    payload = json.loads(session.context.rendered.split("\n", 2)[1])
    assert payload["core_budget"] == {"estimated_tokens": 4, "max_tokens": 4}
    assert session.expected_core == visible
    item = payload["memory"]["items"][0]
    assert item["stored_evidence_ids"] == evidence_ids
    assert item["visible_evidence_ids"] == evidence_ids[:2]
    assert session.context.allowed_evidence_message_ids == frozenset(evidence_ids[:2])


def test_material_units_threshold_age_and_initial_lookback():
    old = memory("Old history", NOW - timedelta(days=20))
    assert inspect_pressure(1, NOW)["total"] == 0
    for i in range(7):
        memory(f"New fact {i}")
    conn = _connect()
    for i in range(7):
        conn.execute(
            "INSERT INTO messages(user_id, role, content, created_at) VALUES (1, 'assistant', ?, ?)",
            (f"Delivered message {i}", NOW.isoformat()),
        )
    conn.commit()
    conn.close()
    status = inspect_pressure(1, NOW)
    assert status["total"] == 7
    assert status["eligible"] is False
    assert inspect_pressure(1, NOW + timedelta(days=6))["eligible"] is False
    assert inspect_pressure(1, NOW + timedelta(days=7))["reason"] == "max_age"
    memory("Eighth new fact")
    assert inspect_pressure(1, NOW)["reason"] == "evidence_pressure"
    batch = prepare_batch(1, NOW)
    assert old not in {item["id"] for item in load_batch(1, batch)["material"]["memory"]}
    complete_batch(1, batch, NOW)
    assert inspect_pressure(1, NOW + timedelta(days=8))["eligible"] is False
    update_memory_item(old, 1, content="Old history corrected", importance=2)
    assert inspect_pressure(1, NOW + timedelta(days=8))["total"] == 1


def test_backlog_does_not_drop_small_tail_or_advance_on_prepare():
    ids = {memory(f"Distinct fact {i}") for i in range(85)}
    batches = []
    reviewed = set()
    for day, size in enumerate((40, 40, 5)):
        now = NOW + timedelta(days=day)
        batch = prepare_batch(1, now)
        batches.append(batch)
        shown = {item["id"] for item in load_batch(1, batch)["material"]["memory"]}
        assert len(shown) == size
        assert shown.isdisjoint(reviewed)
        assert prepare_batch(1, now) == batch
        assert inspect_pressure(1, now)["total"] == 85 - len(reviewed)
        assert claim_attempt(1, now.date().isoformat()) is True
        assert claim_attempt(1, now.date().isoformat()) is False
        complete_batch(1, batch, now)
        reviewed |= shown
    assert len(set(batches)) == 3
    assert reviewed == ids
    assert inspect_pressure(1, NOW + timedelta(days=3))["eligible"] is False


def test_diary_progress_reads_body_only_and_keeps_partial_day(tmp_path):
    from mochi.diary import diary
    archive = diary.path.parent / "diary_archive"
    archive.mkdir()
    body = "My day, not a status row.\n" * 700
    (archive / "2026-10.md").write_text(
        "# Diary 2026-10-02\n\n## 今日状態\n- auto status\n\n## 今日日記\n\n"
        "# Diary 2026-10-03\n\n## 今日状態\n- other status\n\n## 今日日記\n" + body,
        encoding="utf-8",
    )
    status = inspect_pressure(1, NOW)
    assert status["diary"] == 1
    batch = prepare_batch(1, NOW)
    first = load_batch(1, batch)["material"]["diary"][0]
    assert len(first["content"]) == 12_000
    assert first["date"] == "2026-10-03"
    complete_batch(1, batch, NOW)
    assert inspect_pressure(1, NOW + timedelta(days=1))["reason"] == "backlog"
    second_batch = prepare_batch(1, NOW + timedelta(days=1))
    second = load_batch(1, second_batch)["material"]["diary"][0]
    assert second["offset"] == first["next_offset"]
    assert first["content"] + second["content"] == body.strip()
    complete_batch(1, second_batch, NOW + timedelta(days=1))
    assert inspect_pressure(1, NOW + timedelta(days=2))["diary"] == 0


def test_new_evidence_and_concurrent_changes_remain_pending():
    item_id = memory("Tea preference")
    batch = prepare_batch(1, NOW)
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET evidence_message_ids = '[42]' WHERE id=?", (item_id,),
    )
    conn.commit()
    conn.close()
    complete_batch(1, batch, NOW)
    assert inspect_pressure(1, NOW)["memory"] == 1
    next_batch = prepare_batch(1, NOW + timedelta(days=7))
    complete_batch(1, next_batch, NOW + timedelta(days=7))
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET importance = 3, access_count = 12, updated_at = ? WHERE id=?",
        ((NOW + timedelta(days=8)).isoformat(), item_id),
    )
    conn.commit()
    conn.close()
    assert inspect_pressure(1, NOW + timedelta(days=8))["total"] == 0


@pytest.mark.asyncio
async def test_daily_scheduler_defers_busy_chat_and_retries_next_day_only(monkeypatch):
    import mochi.heartbeat as hb
    from mochi.db import claim_scheduled_run, finish_scheduled_run, get_scheduled_run
    from mochi.ai_client import ChatResult

    monkeypatch.setattr(hb, "_active_chat_tokens", {object()})
    monkeypatch.setattr(hb, "_state", hb.SLEEPING)
    values = {
        "WEEKLY_MAINTENANCE_ENABLED": True, "MAINTENANCE_HOUR": 3,
        "WEEKLY_MAINTENANCE_MINUTE": 15, "LLM_HEARTBEAT_TIMEOUT_SECONDS": 120,
    }
    monkeypatch.setattr(hb, "_effective", values.__getitem__)
    memory("Worth considering")
    attempts = []

    async def run(user, day, batch, generation):
        attempts.append(batch)
        return ChatResult(disposition="invalid" if len(attempts) == 1 else "skip")

    monkeypatch.setattr(hb, "_dream_callback", run)
    day = NOW.date().isoformat()
    claim_scheduled_run("nightly", day)
    finish_scheduled_run("nightly", day, success=True)
    assert not await hb._run_dream_if_due(1, NOW)
    monkeypatch.setattr(hb, "_active_chat_tokens", set())
    assert await hb._run_dream_if_due(1, NOW)
    assert get_scheduled_run("dream", day)["status"] == "failed"
    assert not await hb._run_dream_if_due(1, NOW + timedelta(minutes=30))
    tomorrow = NOW + timedelta(days=1)
    next_day = tomorrow.date().isoformat()
    claim_scheduled_run("nightly", next_day)
    finish_scheduled_run("nightly", next_day, success=True)
    assert await hb._run_dream_if_due(1, tomorrow)
    assert attempts[0] == attempts[1]
    assert get_scheduled_run("dream", next_day)["status"] == "success"
