import json
from datetime import datetime, timedelta, timezone

import pytest

from mochi.ai_client import chat
from mochi.core_store import read_core, replace_core
from mochi.db import _connect, get_recent_messages, insert_memory_item
from mochi.main_runtime import MainRuntimeEntry
from mochi.knowledge_graph import (
    curate_relationships,
    list_active_relationships,
)
from mochi.dream import create_dream_session
from mochi.dream_store import prepare_batch
from tests.e2e.mock_llm import make_response, make_tool_call


@pytest.mark.asyncio
async def test_dream_main_updates_core_without_chat_history(
    mock_llm_factory,
):
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Shiki moved to Tokyo and started learning Japanese', "
        "'2026-08-05T10:00:00+00:00', 1)"
    )
    evidence_id = cursor.lastrowid
    conn.commit()
    conn.close()
    item_id = insert_memory_item(
        1,
        "Shiki moved to Tokyo and started learning Japanese",
        2,
        source="extracted",
        evidence_message_ids=[evidence_id],
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET created_at = ?, updated_at = ? WHERE id = ?",
        (
            "2026-08-05T10:05:00+00:00",
            "2026-08-05T10:05:00+00:00",
            item_id,
        ),
    )
    conn.commit()
    item = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    old_core = "# Us\n- Natural companionship"
    replace_core(old_core)
    mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call(
                "update_dream_core",
                {
                    "content": old_core + "\n- User started learning Japanese",
                },
            ),
        ]),
        make_response(tool_calls=[
            make_tool_call(
                "curate_dream_memory",
                {"operations": []},
            ),
        ]),
        make_response(tool_calls=[
            make_tool_call(
                "curate_relationships",
                {
                    "operations": [{
                        "op": "upsert",
                        "subject": {"name": "Shiki", "type": "person"},
                        "predicate": "lives_in",
                        "object": {"name": "Tokyo", "type": "place"},
                        "source_memory": {
                            "item_id": item_id,
                        },
                    }],
                },
            ),
        ]),
        make_response("done"),
    ])

    result = await chat(runtime_entry=MainRuntimeEntry.dream(
        logical_date="2026-08-10",
        period_key=prepare_batch(1, datetime(2026, 8, 10, 4, tzinfo=timezone.utc)),
        user_id=1,
        channel_id=100,
        transport="fake",
    ))

    assert result.text == ""
    assert "User started learning Japanese" in read_core()
    assert [row["role"] for row in get_recent_messages(1)] == ["user"]
    conn = _connect()
    relation = conn.execute(
        "SELECT predicate, source, source_memory_id FROM kg_triples "
        "WHERE valid_to IS NULL"
    ).fetchone()
    conn.close()
    assert dict(relation) == {
        "predicate": "lives_in",
        "source": "dream_main",
        "source_memory_id": item_id,
    }


@pytest.mark.asyncio
async def test_dream_memory_refreshes_relationship_scope():
    conn = _connect()
    original_evidence = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Shiki may move to Tokyo', "
        "'2026-08-05T10:00:00+00:00', 1)"
    ).lastrowid
    new_evidence = conn.execute(
        "INSERT INTO messages "
        "(user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Shiki now lives in Tokyo', "
        "'2026-08-06T10:00:00+00:00', 1)"
    ).lastrowid
    conn.commit()
    conn.close()
    item_id = insert_memory_item(
        1,
        "Shiki may move to Tokyo",
        2,
        source="extracted",
        evidence_message_ids=[original_evidence],
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET created_at = ?, updated_at = ? WHERE id = ?",
        (
            "2026-08-05T10:05:00+00:00",
            "2026-08-05T10:05:00+00:00",
            item_id,
        ),
    )
    conn.commit()
    item = conn.execute(
        "SELECT content, updated_at FROM memory_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    source_snapshot = {
        "item_id": item_id,
        "content": item["content"],
        "updated_at": item["updated_at"],
    }
    relationship_operation = {
        "op": "upsert",
        "subject": {"name": "Shiki", "type": "person"},
        "predicate": "lives_in",
        "object": {"name": "Tokyo", "type": "place"},
        "source_memory": source_snapshot,
    }
    curate_relationships(1, {item_id}, [relationship_operation])
    session = create_dream_session(
        user_id=1,
        logical_date="2026-08-10",
        period_key=prepare_batch(1, datetime(2026, 8, 10, 4, tzinfo=timezone.utc)),
    )

    premature = await session.execute(
        "curate_relationships",
        {"operations": []},
    )
    assert premature.success is True

    memory_result = await session.execute(
        "curate_dream_memory",
        {
            "operations": [{
                "op": "edit",
                "item_id": item_id,
                "content": "Shiki now lives in Tokyo",
                "importance": 2,
                "evidence_message_ids": [new_evidence],
            }],
        },
    )

    assert memory_result.success is True
    refreshed = json.loads(memory_result.output)["relationship_context"]
    assert refreshed["active_relationships"] == []
    assert len(refreshed["memory_items"]) == 1

    relationship_operation["source_memory"] = {"item_id": item_id}
    unseen = await session.execute(
        "curate_relationships", {"operations": [relationship_operation]},
    )
    assert unseen.success is False
    assert "changed after Dream context was built" in unseen.output
    session.advance_visible_context()
    relationship_result = await session.execute(
        "curate_relationships",
        {"operations": [relationship_operation]},
    )

    assert relationship_result.success is True
    active = list_active_relationships(1)
    assert len(active) == 1
    assert active[0]["source_memory_id"] == item_id
    from mochi.dream_store import complete_batch, inspect_pressure
    moment = datetime(2026, 8, 10, 4, tzinfo=timezone.utc)
    complete_batch(1, session.context.period_key, moment)
    assert inspect_pressure(1, moment)["memory"] == 0


@pytest.mark.asyncio
async def test_dream_memory_rejects_concurrent_change_without_model_snapshot_fields():
    conn = _connect()
    evidence_id = conn.execute(
        "INSERT INTO messages (user_id, role, content, created_at, processed) "
        "VALUES (1, 'user', 'Enjoys green tea', '2026-08-05T10:00:00+00:00', 1)"
    ).lastrowid
    conn.commit()
    conn.close()
    item_id = insert_memory_item(
        1, "Enjoys green tea", 1, source="extracted", evidence_message_ids=[evidence_id],
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET created_at=?, updated_at=? WHERE id=?",
        ("2026-08-05T10:05:00+00:00", "2026-08-05T10:05:00+00:00", item_id),
    )
    conn.commit()
    conn.close()
    session = create_dream_session(
        user_id=1, logical_date="2026-08-10",
        period_key=prepare_batch(1, datetime(2026, 8, 10, 4, tzinfo=timezone.utc)),
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET content='Enjoys jasmine tea', updated_at=? WHERE id=?",
        ("2026-08-06T10:05:00+00:00", item_id),
    )
    conn.commit()
    conn.close()

    result = await session.execute("curate_dream_memory", {"operations": [{
        "op": "edit", "item_id": item_id, "content": "Enjoys green tea",
        "importance": 2, "evidence_message_ids": [evidence_id],
    }]})

    assert result.success is False
    conn = _connect()
    row = conn.execute("SELECT content,importance FROM memory_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert tuple(row) == ("Enjoys jasmine tea", 1)


def _new_entry():
    from mochi.config import logical_today
    now = datetime.now(timezone.utc)
    insert_memory_item(1, "A new experience", 1, source="test")
    return MainRuntimeEntry.dream(
        user_id=1, channel_id=100, transport="fake",
        logical_date=logical_today(now), period_key=prepare_batch(1, now),
    )


@pytest.mark.asyncio
async def test_reminder_survives_failed_dream_and_replay_does_not_create_again(mock_llm_factory):
    from mochi.dream_store import load_batch

    entry = _new_entry()
    args = {
        "action": "create", "intent": "Reconsider the trip",
        "remind_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    mock_llm_factory([
        make_response(tool_calls=[make_tool_call("manage_dream_self_reminder", args)]),
        RuntimeError("offline"), RuntimeError("offline"),
    ])
    with pytest.raises(RuntimeError, match="offline"):
        await chat(runtime_entry=entry)
    assert load_batch(1, entry.period_key)["status"] == "pending"
    mock_llm_factory([
        make_response(tool_calls=[make_tool_call("manage_dream_self_reminder", args)]),
        make_response(""),
    ])
    result = await chat(runtime_entry=entry)
    assert result.disposition == "skip"
    assert result.text == ""
    conn = _connect()
    rows = conn.execute("SELECT channel_id, transport, kind, context FROM reminders").fetchall()
    conn.close()
    assert [tuple(row) for row in rows] == [(100, "fake", "self", "Reconsider the trip")]
    assert get_recent_messages(1) == []


@pytest.mark.asyncio
async def test_dream_reminders_reject_invisible_notify_and_started_targets():
    from mochi.skills.reminder.queries import create_reminder, create_self_reminder
    entry = _new_entry()
    when = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    notify_id = create_reminder(1, 100, "Direct notice", when)
    self_id = create_self_reminder(1, 100, "My intention", when, "fake")
    session = create_dream_session(
        user_id=1, logical_date=entry.logical_date, period_key=entry.period_key,
        channel_id=100, transport="fake",
    )
    assert not (await session.execute("manage_dream_self_reminder", {
        "action": "delete", "reminder_id": notify_id,
    })).success
    conn = _connect()
    conn.execute("UPDATE reminders SET attempt_count=1 WHERE id=?", (self_id,))
    conn.commit()
    conn.close()
    await session.execute("manage_dream_self_reminder", {"action": "list"})
    session.advance_visible_context()
    assert not (await session.execute("manage_dream_self_reminder", {
        "action": "delete", "reminder_id": self_id,
    })).success
    conn = _connect()
    assert {row[0] for row in conn.execute("SELECT status FROM reminders")} == {"pending"}
    conn.close()


@pytest.mark.asyncio
async def test_incomplete_dream_is_not_a_completed_batch(mock_llm_factory):
    entry = _new_entry()
    response = make_response("partial")
    response.finish_reason = "length"
    mock_llm_factory([response])
    result = await chat(runtime_entry=entry)
    assert result.disposition == "invalid"
    assert result.text == ""


@pytest.mark.asyncio
async def test_tool_round_exhaustion_and_unresolved_failure_are_not_completion(mock_llm_factory, monkeypatch):
    import mochi.config as ai
    entry = _new_entry()
    monkeypatch.setattr(ai, "TOOL_LOOP_MAX_ROUNDS", 1)
    mock_llm_factory([make_response(tool_calls=[
        make_tool_call("curate_dream_memory", {"operations": []}),
    ])])
    result = await chat(runtime_entry=entry)
    assert result.disposition == "invalid"
    monkeypatch.setattr(ai, "TOOL_LOOP_MAX_ROUNDS", 5)
    mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("manage_dream_self_reminder", {"action": "delete", "reminder_id": 999}),
        ]),
        make_response(""),
    ])
    assert (await chat(runtime_entry=entry)).disposition == "invalid"
    mock_llm_factory([make_response("")])
    assert (await chat(runtime_entry=entry)).disposition == "skip"


@pytest.mark.asyncio
async def test_chat_generation_interrupts_dream_before_tool_execution(mock_llm_factory):
    import mochi.heartbeat as hb
    entry = _new_entry()
    from dataclasses import replace
    entry = replace(entry, chat_generation=hb.chat_activity_generation())
    mock = mock_llm_factory([])

    def chat_arrives(*args, **kwargs):
        token = hb.begin_active_chat()
        hb.end_active_chat(token)
        return make_response(tool_calls=[make_tool_call("update_dream_core", {"content": "stale"})])

    mock.chat = chat_arrives
    original = read_core()
    result = await chat(runtime_entry=entry)
    assert result.disposition == "invalid"
    assert read_core() == original


@pytest.mark.asyncio
async def test_evidence_only_change_rejects_stale_memory_curation():
    entry = _new_entry()
    session = create_dream_session(
        user_id=1, logical_date=entry.logical_date, period_key=entry.period_key,
    )
    item = session.context.package.window_items[0]
    conn = _connect()
    evidence_id = conn.execute(
        "INSERT INTO messages(user_id, role, content, created_at) VALUES (1,'user','A new experience',?)",
        (datetime.now(timezone.utc).isoformat(),),
    ).lastrowid
    conn.execute(
        "UPDATE memory_items SET evidence_message_ids=? WHERE id=?",
        (json.dumps([evidence_id]), item.id),
    )
    conn.commit()
    conn.close()
    result = await session.execute("curate_dream_memory", {"operations": [{
        "op": "edit", "item_id": item.id, "content": item.content,
        "importance": 3, "evidence_message_ids": [],
    }]})
    assert not result.success
    conn = _connect()
    row = conn.execute(
        "SELECT importance,evidence_message_ids FROM memory_items WHERE id=?", (item.id,),
    ).fetchone()
    conn.close()
    assert tuple(row) == (1, json.dumps([evidence_id]))
