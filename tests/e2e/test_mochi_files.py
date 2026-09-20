"""Personal workspace document flow, provenance, and privacy receipts."""

from __future__ import annotations

import json

import pytest

from mochi.ai_client import chat
from mochi.db import get_recent_tool_executions
from mochi.transport import IncomingMessage
from tests.e2e.mock_llm import make_response, make_tool_call


CAPABILITY = (
    "你具备自己开发小工具的能力：可以在 `personal_workspace` 中编写、运行调试"
    "并启用可重复调用的个人扩展，而不只是使用已有工具。"
)
FILES_TOOLS = {"browse_workspace", "edit_workspace"}
WORKSPACE_TOOLS = FILES_TOOLS | {"run_extension", "activate_extension"}


@pytest.mark.asyncio
@pytest.mark.parametrize("escalation", [True, False])
@pytest.mark.parametrize("development", [True, False])
async def test_agent_knows_personal_extensions_before_search_or_unrelated_routing(
    monkeypatch, mock_llm_factory, escalation, development,
):
    """Prove first-request context and availability, not a scripted model's judgment."""
    import mochi.admin.admin_db as admin_db
    import mochi.config as config
    import mochi.skills as registry
    import mochi.tool_router as router
    from mochi.personal_workspace import set_development_enabled

    monkeypatch.setattr(config, "TOOL_ROUTER_ENABLED", True)
    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", escalation)
    monkeypatch.setattr(admin_db, "list_tier_assignments", lambda: {"lite": "fixture"})
    routed = []

    async def unrelated_route(*_args, **kwargs):
        assert "personal_workspace" not in kwargs["catalog"]
        assert "todo" in kwargs["catalog"]
        routed.append("todo")
        return ["todo"]

    monkeypatch.setattr(router, "classify_skills", unrelated_route)
    set_development_enabled(development)
    mock = mock_llm_factory([make_response("I can choose an appropriate approach.")])
    await chat(IncomingMessage(
        user_id=1, channel_id=100, text="I need a capability I do not have yet.",
        transport="fake", owner_authorized=False,
    ))

    assert routed == ["todo"]
    initial = mock.call_log[0]
    prompt = initial["messages"][0]["content"]
    names = {tool["function"]["name"] for tool in initial["tools"]}
    assert prompt.count(CAPABILITY) == 1
    assert 'skills: ["personal_workspace"]' in prompt
    assert 'browse_workspace(action="guide")' in prompt
    assert "由你根据目的判断；资料不必变成程序" in prompt
    assert "实际能执行什么，仍取决于当前设置和本轮可用工具" in prompt
    assert "当 `request_tools` 可用时" in prompt
    assert "不是向用户申请许可" in prompt
    assert "manage_todo" in names
    assert ("request_tools" in names) is escalation
    assert WORKSPACE_TOOLS.isdisjoint(names)
    assert all(message["role"] != "tool" for message in initial["messages"])
    assert "class PersonalSkill" not in prompt
    available = {
        tool["function"]["name"]
        for tool in registry.get_tools_by_names(["personal_workspace"])
    }
    assert available == (WORKSPACE_TOOLS if development else FILES_TOOLS)


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
    reply = await chat(message)
    assert reply.text

    initial_names = {
        tool["function"]["name"] for tool in mock.call_log[0]["tools"]
    }
    assert WORKSPACE_TOOLS.isdisjoint(initial_names)
    initial_prompt = "\n".join(
        item.get("content", "")
        for item in mock.call_log[0]["messages"]
        if isinstance(item.get("content"), str)
    )
    assert initial_prompt.count(CAPABILITY) == 1
    assert "你有一片持久的私人 Markdown 空间" not in initial_prompt
    assert "old_text" not in initial_prompt
    assert "上一版本" not in initial_prompt
    assert "### mochi_files" not in initial_prompt
    assert "### development" not in initial_prompt

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


@pytest.mark.asyncio
async def test_plain_medical_notebook_can_be_saved_without_code_or_health_facts(
    tmp_path, monkeypatch, mock_llm_factory,
):
    """Mechanics only: scripted choices are not evidence of model preference."""
    import mochi.config as config
    import mochi.mochi_files_store as files_store
    from mochi.extensions import store

    monkeypatch.setattr(config, "TOOL_ESCALATION_ENABLED", True)
    monkeypatch.setattr(files_store, "DATA_DIR", tmp_path / "files_data")
    path = "documents/health/病历本.md"
    content = (
        "# 病历本\n\n尚未记录病情。\n\n"
        "## 每次记录可补充\n"
        "- 日期与时间\n- 症状与持续时间\n- 就诊、检查和用药信息（如有）\n"
    )
    agent = mock_llm_factory([
        make_response(tool_calls=[
            make_tool_call("request_tools", {"skills": ["personal_workspace"]}),
        ]),
        make_response(tool_calls=[
            make_tool_call("edit_workspace", {
                "action": "create", "path": path, "content": content,
            }),
        ]),
        make_response(tool_calls=[
            make_tool_call("browse_workspace", {"action": "read", "path": path}),
        ]),
        make_response("病历本已建好。你还没有提供具体病情，想先记录哪一次？"),
    ])
    result = await chat(IncomingMessage(
        user_id=1, channel_id=100, text="建立一个病历本，记录我的生病情况",
        transport="fake", owner_authorized=False,
    ))
    assert result.text
    saved = files_store.DATA_DIR / files_store.ACTIVE_DIRNAME / "health" / "病历本.md"
    assert saved.read_text(encoding="utf-8") == content
    records = get_recent_tool_executions(1, limit=10, state_changes_only=False)
    assert {item["tool_name"] for item in records} == FILES_TOOLS
    assert len(records) == 2
    assert not store.list_extensions()
    receipt = next(
        json.loads(item["content"])
        for item in agent.call_log[-1]["messages"]
        if item.get("role") == "tool"
        and json.loads(item["content"]).get("source") == "agent_authored_document"
    )
    assert json.loads(receipt["result"])["files"][0]["content"] == content
    assert all(content not in item["result_summary"] for item in records)
