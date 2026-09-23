import json
from datetime import datetime, timezone

import pytest

from mochi.ai_client import chat
from mochi.core_store import read_core, replace_core
from mochi.db import _connect, get_recent_messages, insert_memory_item
from mochi.main_runtime import MainRuntimeEntry
from mochi.knowledge_graph import (
    curate_relationships,
    list_active_relationships,
)
from mochi.weekly_maintenance import create_weekly_session
from tests.e2e.mock_llm import make_response, make_tool_call


@pytest.mark.asyncio
async def test_weekly_main_updates_core_without_chat_history(
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
                "update_weekly_core",
                {
                    "content": old_core + "\n- User started learning Japanese",
                },
            ),
        ]),
        make_response(tool_calls=[
            make_tool_call(
                "curate_weekly_memory",
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

    result = await chat(runtime_entry=MainRuntimeEntry.weekly_maintenance(
        logical_date="2026-08-10",
        period_key="2026-W33",
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
        "source": "weekly_main",
        "source_memory_id": item_id,
    }


@pytest.mark.asyncio
async def test_weekly_memory_refreshes_relationship_scope():
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
    session = create_weekly_session(
        user_id=1,
        logical_date="2026-08-10",
        period_key="2026-W33",
    )

    premature = await session.execute(
        "curate_relationships",
        {"operations": []},
    )
    assert premature.success is False

    memory_result = await session.execute(
        "curate_weekly_memory",
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
    assert "changed after Weekly context was built" in unseen.output
    session.advance_visible_context()
    relationship_result = await session.execute(
        "curate_relationships",
        {"operations": [relationship_operation]},
    )

    assert relationship_result.success is True
    active = list_active_relationships(1)
    assert len(active) == 1
    assert active[0]["source_memory_id"] == item_id


@pytest.mark.asyncio
async def test_weekly_memory_rejects_concurrent_change_without_model_snapshot_fields():
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
    session = create_weekly_session(
        user_id=1, logical_date="2026-08-10", period_key="2026-W33",
    )
    conn = _connect()
    conn.execute(
        "UPDATE memory_items SET content='Enjoys jasmine tea', updated_at=? WHERE id=?",
        ("2026-08-06T10:05:00+00:00", item_id),
    )
    conn.commit()
    conn.close()

    result = await session.execute("curate_weekly_memory", {"operations": [{
        "op": "edit", "item_id": item_id, "content": "Enjoys green tea",
        "importance": 2, "evidence_message_ids": [evidence_id],
    }]})

    assert result.success is False
    conn = _connect()
    row = conn.execute("SELECT content,importance FROM memory_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    assert tuple(row) == ("Enjoys jasmine tea", 1)
