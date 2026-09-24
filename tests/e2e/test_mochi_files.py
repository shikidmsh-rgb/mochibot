"""Personal workspace document flow, provenance, and privacy receipts."""

from __future__ import annotations

import json

import pytest

from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions
from mochi.transport import IncomingMessage
from tests.e2e.mock_llm import make_response, make_tool_call


FILES_TOOLS = {"browse_workspace", "edit_workspace"}
WORKSPACE_TOOLS = FILES_TOOLS | {"run_extension", "activate_extension"}


@pytest.mark.asyncio
async def test_personal_workspace_document_vertical_contract(
    tmp_path, monkeypatch, mock_llm_factory,
):
    import mochi.config as config
    import mochi.mochi_files_store as store

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    monkeypatch.setattr(config, "TOOL_LOOP_MAX_ROUNDS", 6)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path / "files_data")
    authored_text = "只有 Main 写下的秘密草稿"
    finished_text = "只有 Main 写下的完整正文 #123"
    mock = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": ["personal_workspace"]}),
        ]),
        make_response(tool_calls=[
            make_tool_call("edit_workspace", {
                "action": "create",
                "path": "documents/letters/first.md",
                "content": authored_text,
            }),
        ]),
        make_response(tool_calls=[
            make_tool_call("edit_workspace", {
                "action": "edit",
                "path": "documents/letters/first.md",
                "old_text": "秘密草稿",
                "new_text": "完整正文 #123",
            }),
        ]),
        make_response(tool_calls=[
            make_tool_call("browse_workspace", {
                "action": "search",
                "path": "documents",
                "query": "完整正文",
            }),
        ]),
        make_response(tool_calls=[
            make_tool_call("browse_workspace", {
                "action": "read",
                "path": "documents/letters/first.md",
            }),
        ]),
        make_response("写好了，也重新打开确认过。"),
    ])

    message = IncomingMessage(
        user_id=1,
        channel_id=100,
        text="写一封信并收好",
        transport="fake",
        owner_authorized=False,
    )
    await chat(message)

    initial_names = {
        tool["function"]["name"] for tool in mock.call_log[0]["tools"]
    }
    assert WORKSPACE_TOOLS.isdisjoint(initial_names)
    second_names = {
        tool["function"]["name"] for tool in mock.call_log[1]["tools"]
    }
    assert second_names - initial_names == WORKSPACE_TOOLS
    request_receipt = next(
        json.loads(item["content"])
        for item in mock.call_log[1]["messages"]
        if item.get("role") == "tool"
        and "loaded" in item.get("content", "")
    )
    assert len(request_receipt["loaded"]) == 1
    assert request_receipt["loaded"][0]["skill"] == "personal_workspace"
    assert set(request_receipt["loaded"][0]["tools"]) == WORKSPACE_TOOLS

    read_receipt = next(
        json.loads(item["content"])
        for item in mock.call_log[5]["messages"]
        if item.get("role") == "tool"
        and finished_text in item.get("content", "")
    )
    assert read_receipt["source"] == "agent_authored_document"
    assert finished_text in read_receipt["result"]
    assert (
        store.DATA_DIR / store.ACTIVE_DIRNAME / "letters" / "first.md"
    ).read_text(encoding="utf-8") == finished_text

    executions = get_recent_tool_executions(
        1, limit=10, state_changes_only=False
    )
    files_executions = [
        item for item in executions if item["tool_name"] in FILES_TOOLS
    ]
    assert {item["tool_name"] for item in files_executions} == FILES_TOOLS
    save_executions = [
        item for item in files_executions
        if item["tool_name"] == "edit_workspace"
    ]
    assert len(save_executions) == 2
    for execution in save_executions:
        for key in ("content", "old_text", "new_text"):
            if key in execution["arguments"]:
                assert execution["arguments"][key] == "[REDACTED]"
        assert execution["state_changed"] is True
    assert all(
        authored_text not in item["result_summary"]
        and finished_text not in item["result_summary"]
        for item in files_executions
    )
    assert all(
        "content" not in item["result_summary"]
        and "excerpt" not in item["result_summary"]
        for item in files_executions
    )
    search_execution = next(
        item for item in files_executions
        if item["tool_name"] == "browse_workspace"
        and item["action"] == "search"
    )
    assert search_execution["arguments"]["query"] == "[REDACTED]"
    assert all(not item["entity_refs"] for item in files_executions)

    from mochi.skills import dispatch

    denied = await dispatch(
        "browse_workspace",
        {"action": "read", "path": "documents/letters/first.md"},
        user_id=1,
        actor="lite",
    )
    assert denied.success is False
    assert denied.error_code == "main_only"
    assert authored_text not in denied.output
