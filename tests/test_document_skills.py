"""Document skills use the real workspace, registry, snapshots and loading state."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from mochi import db, personal_workspace, skills
from mochi.adaptive_tool_load import recalculate, resolve_definition
from mochi.extensions import loader, store, template
from mochi.request_tools import build_catalog, resolve_request
from mochi.skills.base import SkillContext
from mochi.skills.personal_workspace.handler import PersonalWorkspaceSkill
from mochi.tool_availability import ToolAvailability
from mochi.turn_tool_policy import build_router_catalog

NAME = "local_outfit"
TOOL = f"{NAME}_read"
DRAFT = f"extensions/{NAME}/draft"


def _manifest(body: str, *, fields: str = "") -> str:
    return (
        f"---\nname: {NAME}\nmod_api: 1\nkind: document\n"
        f"description: Outfit guidance\ntype: tool\n{fields}---\n\n{body}"
    )


async def _workspace(tool: str, **args):
    return await PersonalWorkspaceSkill().execute(SkillContext(
        trigger="tool_call", actor="main", user_id=1, source="chat",
        tool_name=tool, args=args,
    ))


async def _install(body: str):
    created = await _workspace(
        "edit_workspace", action="create", path=DRAFT,
        files=[{"path": "SKILL.md", "content": _manifest(body)}],
    )
    assert created.success and created.state_changed
    installed = await _workspace("activate_extension", path=DRAFT)
    assert installed.success and installed.state_changed
    return skills.get_skill(NAME)


@pytest.mark.asyncio
async def test_author_route_read_and_discover_document_without_body_injection():
    body = "# Outfit\n\n" + "Layer for changing temperatures.\n" * 500
    body += "\n## Tools\n\n### notes, not a schema\n\n## Capability Context\n\nPersonal notes.\n"
    skill = await _install(body)
    files = store.inspect_package(store.ROOT / NAME / "current")
    assert [item["path"] for item in files] == ["SKILL.md"]
    assert NAME in build_router_catalog()
    assert TOOL in {item["function"]["name"] for item in skills.get_tools_by_load("routed")}
    assert skills.get_capability_context_for_tools([TOOL]) == ""
    assert body not in json.dumps(build_catalog().eligible[NAME].definitions)

    first = await skills.dispatch(TOOL, {}, actor="main")
    page = json.loads(first.output)
    assert first.output == json.dumps(page, ensure_ascii=False, separators=(",", ":"))
    assert first.success and not first.state_changed
    assert page["content"] == body[:store.MAX_READ_CHARS]
    assert page["truncated"] and not page["complete"]
    second = await skills.dispatch(TOOL, {"offset": page["next_offset"]}, actor="main")
    remaining = json.loads(second.output)
    assert page["content"] + remaining["content"] == body
    assert remaining["version"] == page["version"]
    assert remaining["complete"] and remaining["next_offset"] is None
    for args in ({"offset": -1}, {"limit": True}, {"limit": store.MAX_READ_CHARS + 1}, {"path": "other.md"}):
        rejected = await skills.dispatch(TOOL, args, actor="main")
        assert not rejected.success and not rejected.state_changed
    assert json.loads((await skills.dispatch(TOOL, {"offset": len(body) + 1})).output)["complete"]

    db.save_adaptive_tool_load_state(
        TOOL, effective_load="on_demand", changed_at=datetime.now(timezone.utc).isoformat(),
        pinned_load=None, reason="",
    )
    assert NAME not in build_router_catalog()
    result, additions = resolve_request({"query": "Outfit"}, ToolAvailability(), transport="")
    assert [item["skill"] for item in result["loaded"]] == [NAME]
    availability = ToolAvailability.from_definitions(additions, source="test")
    assert availability.binding_for(TOOL) is skill


@pytest.mark.asyncio
async def test_updates_preserve_bound_reads_published_versions_and_loading_history(monkeypatch):
    body = "# Original\n\n" + "Old guidance.\n" * 10
    first = await _install(body)
    availability = ToolAvailability.from_definitions(first.get_tools(), source="test")
    old_version = json.loads((await skills.dispatch(TOOL, {})).output)["version"]
    now = datetime.now(timezone.utc)
    recalculate(first.get_tools(), user_id=1, now=now)
    recalculate(first.get_tools(), user_id=1, now=now + timedelta(days=31))
    before = db.get_adaptive_tool_load_states()[TOOL]
    assert before["effective_load"] == "on_demand"

    replacement = "# Revised\n\nNew guidance.\n"
    store.edit_file(NAME, "SKILL.md", body, replacement)
    assert json.loads((await skills.dispatch(TOOL, {})).output)["content"] == body
    skills.activate_extension(NAME)
    old_read = await skills.dispatch(TOOL, {}, bound_skill=availability.binding_for(TOOL))
    refreshed = availability.refresh_extensions()
    new_read = await skills.dispatch(TOOL, {}, bound_skill=refreshed.binding_for(TOOL))
    assert json.loads(old_read.output)["content"] == body
    assert json.loads(old_read.output)["version"] == old_version
    assert json.loads(new_read.output)["content"] == replacement
    assert json.loads(new_read.output)["version"] != old_version
    assert (store.ROOT / NAME / "previous" / "SKILL.md").read_text(encoding="utf-8") == _manifest(body)
    assert db.get_adaptive_tool_load_states()[TOOL] == before

    installed = skills.get_skill(NAME)
    store.write_file(NAME, "SKILL.md", _manifest("  \n"))
    rejected = await _workspace("activate_extension", path=DRAFT)
    assert not rejected.success and not rejected.state_change_unknown
    assert skills.get_skill(NAME) is installed
    assert (store.ROOT / NAME / "current" / "SKILL.md").read_text(encoding="utf-8") == _manifest(replacement)

    monkeypatch.delitem(skills._skills, NAME)
    assert skills.load_installed_extension(NAME)
    assert json.loads((await skills.dispatch(TOOL, {})).output)["content"] == replacement
    assert resolve_definition(skills.get_skill(NAME).get_tools()[0])["_load"] == "on_demand"
    assert db.get_adaptive_tool_load_states()[TOOL] == before


@pytest.mark.asyncio
async def test_existing_development_skill_and_transport_boundaries_apply():
    skill = await _install("Guidance.\n")
    personal_workspace.set_development_enabled(False)
    assert (await skills.dispatch(TOOL, {}, actor="main")).success
    denied_edit = await _workspace(
        "edit_workspace", action="append", path=f"{DRAFT}/SKILL.md", content="New text.",
    )
    denied_activation = await _workspace("activate_extension", path=DRAFT)
    assert denied_edit.error_code == denied_activation.error_code == "development_disabled"
    assert not denied_edit.success and not denied_activation.success

    skill.exclude_transports = ["wechat"]
    assert NAME not in build_router_catalog("wechat")
    assert build_catalog("wechat").unavailable[NAME] == "transport_unsupported"
    assert (await skills.dispatch(TOOL, {}, transport="wechat")).error_code == "transport_unavailable"
    db.set_skill_enabled(NAME, False)
    assert NAME not in build_router_catalog()
    assert build_catalog().unavailable[NAME] == "disabled"
    result = await skills.dispatch(TOOL, {}, bound_skill=skill)
    assert not result.success and result.error_code == "skill_disabled"


@pytest.mark.asyncio
async def test_document_validation_prevents_code_execution_and_keeps_failed_drafts(tmp_path):
    sentinel = tmp_path / "imported"
    draft = store.scaffold(NAME, files={
        "SKILL.md": _manifest("Guidance.\n"),
        "handler.py": f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n",
    })
    result = await _workspace("activate_extension", path=DRAFT)
    assert draft["has_draft"]
    assert not result.success and result.error_code == "invalid_document_package"
    assert not result.state_change_unknown
    assert not sentinel.exists() and skills.get_skill(NAME) is None
    assert not (store.ROOT / NAME / "current").exists()
    store.remove_file(NAME, "handler.py")
    store.write_file(NAME, "SKILL.md", _manifest("Guidance.\n", fields="config:\n  KEY:\n    type: str\n    default: value\n"))
    with pytest.raises(store.ExtensionError) as rejected:
        loader.validate_package(NAME, store.ROOT / NAME / "draft")
    assert rejected.value.code == "invalid_document"


@pytest.mark.asyncio
async def test_document_metadata_inspection_and_python_scaffolding_remain_separate():
    store.scaffold(NAME, files={"SKILL.md": _manifest("Guidance.\n")})
    info = next(item for item in skills.get_skill_info_all() if item["name"] == NAME)
    assert not info["loaded"] and info["activation_required"]
    assert info["tools"] == [TOOL]
    assert skills.get_skill(NAME) is None

    python_name = "local_writer"
    files = template.files(python_name)
    files["handler.py"] = (
        "from mochi.mod_api.v1 import Skill, SkillResult\n\n"
        "class Writer(Skill):\n"
        "    async def execute(self, context):\n"
        "        (self.data_dir / 'saved.txt').write_text(context.args['text'], encoding='utf-8')\n"
        "        return SkillResult(state_changed=True)\n"
    )
    store.scaffold(python_name, files=files)
    skills.activate_extension(python_name)
    result = await skills.dispatch(f"{python_name}_echo", {"text": "persistent"})
    assert result.success and result.state_changed
    assert (store.ROOT / python_name / "data" / "saved.txt").read_text(encoding="utf-8") == "persistent"
    assert (store.ROOT / NAME / "draft" / "SKILL.md").read_text(encoding="utf-8") == _manifest("Guidance.\n")
