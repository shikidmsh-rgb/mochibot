import json

import pytest

from mochi import config, db
from mochi.ai_client import chat
from mochi.core_store import read_core, replace_core
from mochi.main_runtime import MainRuntimeEntry
from mochi.transport import IncomingMessage
from tests.mock_llm import make_response, make_tool_call


@pytest.mark.asyncio
async def test_delivery_history_becomes_background_for_the_next_activation(mock_llm_factory, monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_TURNS", 1)
    db.save_message(1, "user", "An older question.", turn_id="old")
    boundary = db.save_message(1, "assistant", "An older answer.", turn_id="old")
    db.save_message(1, "user", "A recent question.", turn_id="recent")
    db.save_message(1, "assistant", "A recent answer.", turn_id="recent")
    source_time = "2026-09-01T03:04:00+00:00"
    with db._connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at=? WHERE turn_id=?",
            (source_time, "old"),
        )
    claim = db.get_conversation_summary_batch(1, 2, retained_turns=1)
    assert claim["next_through_message_id"] == boundary
    summary = "The earlier question was resolved."
    assert db.save_conversation_summary(claim, summary)
    context = db.get_conversation_context(1, 1)
    assert context["summary_source_time"] == source_time
    assert db.get_conversation_context(2, 1)["summary_source_time"] is None
    existing = db.get_recent_messages(1)

    response = make_response("A prepared reply.")
    response.reasoning_content = "OLD_REASONING"
    response.reasoning_source = "test-model"
    client = mock_llm_factory([response, make_response("[SKIP]")])
    monkeypatch.setattr(type(client), "reasoning_source", property(lambda _: "test-model"))
    result = await chat(IncomingMessage(
        user_id=1, channel_id=1, transport="fake", text="An owner message.",
        owner_authorized=True,
    ))
    messages = client.call_log[0]["messages"]
    assert [item["role"] for item in messages] == [
        "system", "user", "user", "assistant", "user", "user",
    ]
    assert summary in messages[1]["content"]
    assert source_time[:16].replace("T", " ") in messages[1]["content"]
    assert sum(summary in item["content"] for item in messages) == 1
    assert [item["content"] for item in messages[2:4]] == [
        row["content"] for row in context["recent"]
    ]
    assert messages[-1]["content"] == "An owner message."
    pending = db.get_recent_messages(1)
    assert pending[:-1] == existing and pending[-1]["role"] == "user"
    assert result.confirm_delivered()
    assert not result.confirm_delivered()
    delivered = db.get_recent_messages(1)
    assert delivered[:-2] == existing
    assert [row["role"] for row in delivered[-2:]] == ["user", "assistant"]
    entry = MainRuntimeEntry(
        kind="self_reminder", user_id=1, channel_id=1, transport="fake",
        intent="CURRENT_EVENT", scheduled_for="2099-01-01T23:00:00+00:00",
    )
    await chat(runtime_entry=entry)
    assert len(client.call_log) == 2
    messages = client.call_log[1]["messages"]
    assert [item["role"] for item in messages] == ["system", "user"]
    assert entry.intent in messages[-1]["content"]
    assert entry.scheduled_for in messages[-1]["content"]
    assert entry.intent not in messages[0]["content"]
    assert summary in messages[0]["content"]
    records = json.loads(messages[0]["content"].split(
        '<recent_completed_turns role="read_only_evidence">\n', 1,
    )[1].split("\n</recent_completed_turns>", 1)[0])
    assert records == [
        {"speaker": row["role"], "timestamp": row["created_at"], "content": row["content"]}
        for row in delivered[2:]
    ]
    assert "OLD_REASONING" not in json.dumps(messages)
    assert db.get_recent_messages(1) == delivered
    db.set_context_reset(1)
    cleared = db.get_conversation_context(1, 1)
    assert cleared["summary"] == "" and cleared["summary_source_time"] is None
    assert db.get_recent_messages(1) == delivered


@pytest.mark.asyncio
async def test_only_authorized_complete_tool_calls_reach_execution(mock_llm_factory):
    replace_core("Keep this Core.")
    memory_id = db.save_memory_item(1, "Keep this memory.")
    malformed = make_tool_call("update_core", None, "malformed")
    malformed["argument_error"] = "invalid JSON"
    incomplete = make_response(tool_calls=[
        make_tool_call("update_core", {"content": "Must not run"}, "incomplete"),
    ])
    incomplete.tool_calls_complete = False
    client = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": ["view_core_memory"]}),
        ]),
        make_response(tool_calls=[
            make_tool_call("view_core_memory", {}, "allowed"),
            make_tool_call("delete_memory", {"memory_id": memory_id}, "unavailable"),
            make_tool_call("update_core", {"content": 123}, "invalid"),
            malformed,
        ]),
        incomplete,
        make_response("Finished."),
    ])
    await chat(IncomingMessage(
        user_id=1, channel_id=1, transport="fake", text="Read your Core.",
        owner_authorized=True,
    ))
    assert len(client.call_log) == 4
    results = {
        item["tool_call_id"]: json.loads(item["content"])
        for item in client.call_log[-1]["messages"] if item["role"] == "tool"
    }
    assert results["allowed"]["ok"]
    for call_id, code in {
        "unavailable": "tool_not_available_this_turn",
        "invalid": "invalid_tool_arguments",
        "malformed": "malformed_tool_arguments",
        "incomplete": "incomplete_tool_call",
    }.items():
        assert results[call_id]["code"] == code
        assert not results[call_id]["started"] and not results[call_id]["changed"]
    executions = db.get_recent_tool_executions(1, state_changes_only=False, include_failures=True)
    assert [row["tool_name"] for row in executions] == ["view_core_memory"]
    assert read_core() == "Keep this Core."
    with db._connect() as conn:
        assert conn.execute("SELECT id FROM memory_items").fetchone()[0] == memory_id
