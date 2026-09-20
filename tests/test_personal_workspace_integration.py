"""Single-entry discovery, settings, budgets and provenance across Main boundaries."""

import json

import pytest
from fastapi.testclient import TestClient

import mochi.skills as registry
from mochi.db import set_skill_enabled
from mochi.request_tools import ToolLoopBudget, build_catalog, resolve_request
from mochi.tool_availability import ToolAvailability
from mochi.tool_execution import outcome_for, sanitize_arguments


NAMESPACE = "personal_workspace"
FILE_TOOLS = {"browse_workspace", "edit_workspace"}
EXECUTION_TOOLS = {"run_extension", "activate_extension"}


def _names(definitions):
    return {item["function"]["name"] for item in definitions}


def test_one_personal_entry_preserves_diary_and_legacy_discovery():
    registry.discover()
    catalog = build_catalog()
    assert NAMESPACE in catalog.eligible
    assert {"development", "mochi_files"}.isdisjoint(registry.all_skills())
    assert registry.skill_for_tool("write_diary") == "workspace"
    assert registry.skill_for_tool("read_diary") == "workspace"
    assert registry.skill_for_tool("write_extension") is None
    assert registry.skill_for_tool("save_mochi_file") is None

    result, tools = resolve_request(
        {"skills": ["mochi_files", "write_extension"]}, ToolAvailability(),
    )
    assert result["loaded"] == [{
        "skill": NAMESPACE,
        "tools": [tool["function"]["name"] for tool in tools],
    }]
    assert result["renamed"] == [
        {"request": "mochi_files", "skill": NAMESPACE},
        {"request": "write_extension", "skill": NAMESPACE},
    ]
    assert _names(tools) == FILE_TOOLS | EXECUTION_TOOLS
    assert FILE_TOOLS.isdisjoint(_names(registry.get_tools_by_load("resident")))


def test_disabled_startup_keeps_ownership_but_hides_execution():
    set_skill_enabled("development", False)
    registry.discover()
    assert registry.skill_for_tool("run_extension") == NAMESPACE
    assert _names(registry.get_tools_by_names([NAMESPACE])) == FILE_TOOLS
    result, tools = resolve_request({"skills": ["run_extension"]}, ToolAvailability())
    assert not tools
    assert result["unavailable"] == [{"request": "run_extension", "reason": "disabled"}]
    info = next(item for item in registry.get_skill_info_all() if item["name"] == NAMESPACE)
    assert info["enabled"] and not info["development_enabled"]
    assert set(info["tools"]) == FILE_TOOLS

    from mochi.personal_workspace import set_development_enabled

    assert set_development_enabled(True)
    assert _names(registry.get_tools_by_names([NAMESPACE])) == FILE_TOOLS | EXECUTION_TOOLS
    assert registry.skill_for_tool("run_extension") == NAMESPACE


@pytest.mark.asyncio
async def test_midturn_disable_updates_schemas_and_blocks_stale_execution():
    from mochi.personal_workspace import set_development_enabled

    registry.discover()
    old_round = ToolAvailability.from_definitions(
        registry.get_tools_by_names([NAMESPACE]), source="request",
    )
    assert old_round.allows("run_extension")
    set_development_enabled(False)
    assert old_round.allows("run_extension")
    next_round = old_round.refresh_extensions()
    assert next_round.names == FILE_TOOLS

    blocked = await registry.dispatch(
        "run_extension", {"path": "extensions/local_stale/draft"}, actor="main",
    )
    assert not blocked.success
    assert blocked.error_code == "development_disabled"
    set_development_enabled(True)
    assert next_round.refresh_extensions().names == FILE_TOOLS
    receipt, additions = resolve_request({"skills": [NAMESPACE]}, next_round)
    assert receipt["ok"] and _names(additions) == EXECUTION_TOOLS


def test_pooled_file_allowances_preserve_total_and_other_tool_limits():
    budget = ToolLoopBudget()
    for _ in range(6):
        assert budget.claim_tool("edit_workspace", total_limit=8, per_tool_limit=3) is None
    assert budget.claim_tool(
        "edit_workspace", total_limit=8, per_tool_limit=3,
    )["code"] == "per_tool_limit_reached"
    assert budget.claim_tool("run_extension", total_limit=8, per_tool_limit=3) is None
    assert budget.claim_tool("activate_extension", total_limit=8, per_tool_limit=3) is None
    assert budget.claim_tool(
        "browse_workspace", total_limit=8, per_tool_limit=3,
    )["code"] == "tool_call_limit_reached"

    limited = ToolLoopBudget()
    assert limited.claim_tool("browse_workspace", total_limit=8, per_tool_limit=1) is None
    assert limited.claim_tool("browse_workspace", total_limit=8, per_tool_limit=1) is None
    assert limited.claim_tool(
        "browse_workspace", total_limit=8, per_tool_limit=1,
    )["code"] == "per_tool_limit_reached"
    assert limited.claim_tool("run_extension", total_limit=8, per_tool_limit=1) is None
    assert limited.claim_tool(
        "run_extension", total_limit=8, per_tool_limit=1,
    )["code"] == "per_tool_limit_reached"


@pytest.mark.asyncio
async def test_admin_and_conversation_control_code_not_documents(monkeypatch):
    from mochi.admin.admin_server import app
    from mochi.skills.skill_management.handler import SkillManagementSkill
    import mochi.config as config

    registry.discover()
    monkeypatch.setattr(config, "ADMIN_TOKEN", "workspace-test-token")
    client = TestClient(app, headers={"Authorization": "Bearer workspace-test-token"})
    assert client.put(
        "/api/skills/development/enabled", json={"enabled": "false"},
    ).status_code == 400
    assert client.put(
        "/api/skills/personal_workspace/enabled", json={"enabled": False},
    ).status_code == 400
    response = client.put("/api/skills/development/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["documents_available"]
    assert response.json()["workspace"] == NAMESPACE
    names = {item["name"] for item in client.get("/api/skills").json()["skills"]}
    assert {"development", "mochi_files"}.isdisjoint(names)

    saved = await registry.dispatch(
        "edit_workspace",
        {"action": "create", "path": "documents/record.md", "content": "No symptoms supplied."},
        actor="main",
    )
    assert saved.success
    denied = await registry.dispatch(
        "edit_workspace", {"action": "create", "path": "extensions/local_off/draft"},
        actor="main",
    )
    assert not denied.success and denied.error_code == "development_disabled"
    assert SkillManagementSkill()._toggle_skill("development", True).success
    info = next(
        item for item in client.get("/api/skills").json()["skills"] if item["name"] == NAMESPACE
    )
    assert info["development_enabled"]


@pytest.mark.asyncio
async def test_workspace_content_does_not_become_audit_facts():
    registry.discover()
    content = "Private record #123; this is not a database identifier."
    args = {"action": "create", "path": "documents/record.md", "content": content}
    saved = await registry.dispatch("edit_workspace", args, actor="main")
    assert saved.success
    read = await registry.dispatch(
        "browse_workspace", {"action": "read", "path": "documents/record.md"}, actor="main",
    )
    assert read.success and read.content_source == "agent_authored_document"
    outcome = outcome_for(NAMESPACE, "browse_workspace", {"action": "read"}, read)
    assert outcome["entity_refs"] == []
    assert content not in outcome["result_summary"]
    assert sanitize_arguments("edit_workspace", args)["content"] == "[REDACTED]"
    batch = sanitize_arguments("edit_workspace", {
        "action": "create", "path": "extensions/local_private/draft",
        "files": [{"path": "handler.py", "content": content}],
    })
    assert content not in json.dumps(batch)
    assert sanitize_arguments("browse_workspace", {"query": content})["query"] == "[REDACTED]"
