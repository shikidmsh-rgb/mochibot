"""Personal workspace availability, external discovery and management boundaries."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

import mochi.skills as registry
from mochi.db import get_disabled_skills, set_skill_enabled
from mochi.extensions import store, template
from mochi.skills.base import Skill, SkillContext
from mochi.skills.skill_management.handler import SkillManagementSkill


def _client(monkeypatch):
    from mochi.admin.admin_server import app
    import mochi.config as config

    monkeypatch.setattr(config, "ADMIN_TOKEN", "test-extension-admin")
    return TestClient(app, headers={
        "Authorization": "Bearer test-extension-admin", "Origin": "http://localhost",
    })


def _restart():
    registry._external_discovered = False
    registry._external_errors.clear()
    for name, skill in list(registry._skills.items()):
        if skill.external:
            registry._skills.pop(name)
            for tool in skill.tool_names():
                registry._tool_map.pop(tool, None)
    registry.discover()


def test_development_defaults_on_and_explicit_disable_persists(monkeypatch):
    from mochi.db import set_skill_config

    registry.discover()
    assert "development" not in get_disabled_skills()
    assert {tool["function"]["name"] for tool in registry.get_tools_by_names(
        ["personal_workspace"],
    )} == {"browse_workspace", "edit_workspace", "run_extension", "activate_extension"}
    workspace = registry.get_skill("personal_workspace")
    assert workspace.description.strip('"') in registry.get_capability_summary()
    assert registry.get_skill("development") is None
    assert registry.get_skill("mochi_files") is None
    set_skill_config("development", "_enabled", "true")
    assert "development" not in get_disabled_skills()

    client = _client(monkeypatch)
    denied_http = TestClient(client.app).put(
        "/api/skills/development/enabled", json={"enabled": False},
    )
    assert denied_http.status_code in (401, 403)
    assert "development" not in get_disabled_skills()
    assert client.put(
        "/api/skills/development/enabled", json={"enabled": "true"},
    ).status_code == 400
    response = client.put("/api/skills/development/enabled", json={"enabled": False})
    assert response.status_code == 200
    assert "development" in get_disabled_skills()
    _restart()
    assert "development" in get_disabled_skills()
    assert {tool["function"]["name"] for tool in registry.get_tools_by_names(
        ["personal_workspace"],
    )} == {"browse_workspace", "edit_workspace"}
    assert workspace.description.strip('"') in registry.get_capability_summary()
    info = next(item for item in client.get("/api/skills").json()["skills"]
                if item["name"] == "personal_workspace")
    assert info["development_enabled"] is False
    response = client.put("/api/skills/development/enabled", json={"enabled": True})
    assert response.status_code == 200
    _restart()
    assert "development" not in get_disabled_skills()


@pytest.mark.asyncio
async def test_activation_is_live_persistent_and_disable_is_live():
    registry.discover()
    name = "local_echo"
    store.scaffold(name)
    info = next(s for s in registry.get_skill_info_all() if s["name"] == name)
    assert info["activation_required"]
    assert registry.get_skill(name) is None
    result = registry.activate_extension(name)
    assert result["active"] and result["persisted"]
    info = next(s for s in registry.get_skill_info_all() if s["name"] == name)
    assert not info["activation_required"]
    assert info["loaded"]
    skill = registry.get_skill(name)
    assert skill is not None and skill.external
    assert skill.data_dir == store.extension_root(name) / "data"
    first = skill
    registry.discover()
    assert registry.get_skill(name) is first
    set_skill_enabled(name, False)
    result = await registry.dispatch(
        next(iter(skill.tool_names())), {}, actor="main",
    )
    assert result.error_code == "skill_disabled"
    assert registry.get_tools_by_names([name]) == []
    set_skill_enabled("development", False)
    set_skill_enabled(name, True)
    assert registry.get_tools_by_names([name])
    _restart()
    assert registry.get_skill(name) is not first
    assert registry.get_tools_by_names([name])


def test_disabled_extension_is_not_imported_and_failed_package_can_be_disabled(monkeypatch):
    name = "local_broken"
    files = template.files(name)
    files["handler.py"] = "raise RuntimeError('broken extension import')\n"
    store.scaffold(name, handler_py=files["handler.py"])
    store.publish(name, store.extension_root(name) / "draft")
    set_skill_enabled(name, False)
    _restart()
    assert name not in registry._external_errors
    assert registry.get_skill(name) is None
    enabled = SkillManagementSkill()._toggle_skill(name, True)
    assert not enabled.success and enabled.state_changed
    assert enabled.state_change_unknown
    assert "broken extension import" in registry._external_errors[name]
    client = _client(monkeypatch)
    infos = client.get("/api/skills").json()["skills"]
    info = next(item for item in infos if item["name"] == name)
    assert not info["loaded"]
    assert info["load_error"]
    assert client.put(f"/api/skills/{name}/enabled", json={"enabled": False}).status_code == 200
    assert name in get_disabled_skills()


def test_external_config_works_before_loading_and_masks_secrets(monkeypatch):
    name = "local_configured"
    files = template.files(name)
    files["SKILL.md"] = files["SKILL.md"].replace(
        "type: tool",
        "type: tool\nrequires_config: [ACCESS_TOKEN]\nconfig:\n"
        "  ACCESS_TOKEN:\n    type: str\n    default: \"\"\n"
        "    secret: true\n    description: Service token",
    )
    handler = files["handler.py"].replace(
        "    async def execute",
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.constructor_token = self.get_config('ACCESS_TOKEN')\n\n"
        "    async def execute",
    )
    store.scaffold(name, skill_md=files["SKILL.md"], handler_py=handler)
    set_skill_enabled(name, False)
    _restart()
    client = _client(monkeypatch)
    secret = "test-private-extension-credential"
    response = client.put(
        f"/api/skills/{name}/config", json={"key": "ACCESS_TOKEN", "value": secret},
    )
    assert response.status_code == 200
    assert secret not in response.text
    info = next(s for s in client.get("/api/skills").json()["skills"] if s["name"] == name)
    assert info["config_status"]["ACCESS_TOKEN"]
    assert info["config_schema"][0]["secret"]
    assert secret not in json.dumps(info)
    set_skill_enabled(name, True)
    registry.activate_extension(name)
    skill = registry.get_skill(name)
    assert skill.config["ACCESS_TOKEN"] == secret
    assert skill.constructor_token == secret
    assert registry.get_missing_config(skill) == []
    assert client.put(
        f"/api/skills/{name}/config", json={"key": "ACCESS_TOKEN", "value": ""},
    ).status_code == 200
    assert registry.get_missing_config(skill) == ["ACCESS_TOKEN"]


def test_restored_installed_package_loads_only_on_explicit_enable():
    name = "local_restored"
    store.scaffold(name)
    store.publish(name, store.extension_root(name) / "draft")
    _restart()
    root = store.extension_root(name)
    hidden = root.with_name("_" + name)
    root.rename(hidden)
    _restart()
    assert registry.get_skill(name) is None
    hidden.rename(root)
    registry.discover()
    assert registry.get_skill(name) is None
    result = SkillManagementSkill()._toggle_skill(name, True)
    assert result.success
    assert registry.get_skill(name) is not None


def test_registration_conflict_does_not_publish_a_partial_skill():
    class CollisionSkill(Skill):
        async def execute(self, context):
            raise AssertionError("must not execute")

    skill = CollisionSkill()
    skill._name = "local_conflict"
    skill.external = True
    skill._skill_md = {"tools": [
        {"function": {"name": "local_conflict_new"}},
        {"function": {"name": "request_tools"}},
    ]}
    with pytest.raises(ValueError, match="Reserved"):
        registry._register_skill(skill)
    assert "local_conflict" not in registry._skills
    assert "local_conflict_new" not in registry._tool_map


def test_required_config_needs_an_injectable_schema():
    name = "local_config_schema"
    metadata = template.files(name)["SKILL.md"].replace(
        "type: tool", "type: tool\nrequires_config: [ACCESS_TOKEN]",
    )
    store.scaffold(name, skill_md=metadata)
    with pytest.raises(store.ExtensionError, match="config declaration"):
        registry.activate_extension(name)


def test_unsafe_package_path_remains_manageable(monkeypatch):
    name = "local_unsafe"
    store.scaffold(name)

    def blocked_path(extension_id):
        raise store.ExtensionError("invalid_path", "Unsafe package path")

    monkeypatch.setattr(store, "extension_root", blocked_path)
    client = _client(monkeypatch)
    response = client.get("/api/skills")
    assert response.status_code == 200
    info = next(item for item in response.json()["skills"] if item["name"] == name)
    assert "Unsafe package path" in info["load_error"]
    assert client.put(f"/api/skills/{name}/enabled", json={"enabled": False}).status_code == 200
    assert name in get_disabled_skills()


@pytest.mark.asyncio
async def test_run_report_failure_is_not_a_successful_development_call(monkeypatch):
    from mochi.extensions import runner

    registry.discover()

    async def report_failure(*args, **kwargs):
        return {
            "success": True, "report_saved": False,
            "report_error": "Disk is full", "state_change_unknown": True,
        }

    monkeypatch.setattr(runner, "run_extension", report_failure)
    result = await registry.dispatch(
        "run_extension", {"path": "extensions/local_report/draft"}, actor="main",
    )
    assert not result.success
    assert result.error_code == "run_report_failed"
    assert not result.state_changed
    assert result.state_change_unknown
    assert "Disk is full" in result.output
    assert "script completed; run report not saved" in result.summary


@pytest.mark.asyncio
async def test_main_can_develop_without_owner_authorization():
    registry.discover()
    created = await registry.dispatch(
        "edit_workspace", {"action": "create", "path": "extensions/local_no_approval/draft"},
        actor="main", owner_authorized=False,
    )
    assert created.success and created.state_changed
    activated = await registry.dispatch(
        "activate_extension", {"path": "extensions/local_no_approval/draft"},
        actor="main", owner_authorized=False,
    )
    assert activated.success
    set_skill_enabled("development", False)
    denied = await registry.dispatch(
        "edit_workspace", {"action": "create", "path": "extensions/local_off/draft"},
        actor="main", owner_authorized=False,
    )
    assert not denied.success and denied.error_code == "development_disabled"
    assert not store.extension_root("local_off").exists()
    restored = SkillManagementSkill()._toggle_skill("development", True)
    assert restored.success and restored.state_changed
    assert "development" not in get_disabled_skills()


@pytest.mark.asyncio
async def test_activation_failure_preserves_working_registration_and_files():
    name = "local_keep"
    store.scaffold(name)
    registry.activate_extension(name)
    old = registry.get_skill(name)
    current = store.read_files(name, ["handler.py"], area="current")["files"]
    store.write_file(name, "handler.py", "raise RuntimeError('bad candidate')\n")
    with pytest.raises(store.ExtensionError, match="bad candidate") as failure:
        registry.activate_extension(name)
    assert failure.value.state_change_unknown
    assert registry.get_skill(name) is old
    assert store.read_files(name, ["handler.py"], area="current")["files"] == current
    result = await registry.dispatch(name + "_echo", {"text": "still works"})
    assert result.success and result.output == "still works"


def test_publication_failure_never_swaps_live_code(monkeypatch):
    name = "local_publish_fail"
    store.scaffold(name)
    registry.activate_extension(name)
    old = registry.get_skill(name)
    original_replace = store.os.replace

    def fail_publish(source, target):
        if Path(source).name.startswith("_publish_"):
            raise OSError("Cannot install candidate")
        return original_replace(source, target)

    monkeypatch.setattr(store.os, "replace", fail_publish)
    with pytest.raises(store.ExtensionError, match="Cannot install candidate"):
        registry.activate_extension(name)
    assert registry.get_skill(name) is old
    assert (store.extension_root(name) / "current" / "handler.py").is_file()


@pytest.mark.asyncio
async def test_in_flight_call_keeps_old_late_imports_across_updates():
    name = "local_inflight"
    source = template.files(name)["handler.py"]
    source = source.replace(
        'return SkillResult(output=text)',
        'await self.entered()\n'
        '        from .helper import VALUE\n'
        '        return SkillResult(output=VALUE)',
    )
    store.scaffold(name, handler_py=source)
    store.write_file(name, "helper.py", "VALUE = 'old'\n")
    registry.activate_extension(name)
    old = registry.get_skill(name)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    old.entered = pause
    pending = asyncio.create_task(registry.dispatch(name + "_echo", {"text": "go"}))
    await asyncio.wait_for(entered.wait(), 2)
    for version in ("middle", "new"):
        store.write_file(name, "helper.py", f"VALUE = '{version}'\n")
        registry.activate_extension(name)
    release.set()
    result = await pending
    assert result.output == "old"

    async def immediate():
        pass

    registry.get_skill(name).entered = immediate
    current = await registry.dispatch(name + "_echo", {"text": "go"})
    assert current.output == "new"


@pytest.mark.asyncio
async def test_provider_snapshot_pins_schema_and_code_until_next_round():
    from mochi.tool_availability import ToolAvailability

    name = "local_schema"
    files = template.files(name)
    store.scaffold(name)
    registry.activate_extension(name)
    before = ToolAvailability.from_definitions(registry.get_tools_by_names([name]), source="test")
    store.write_file(name, "SKILL.md", files["SKILL.md"].replace("| text |", "| value |"))
    store.write_file(name, "handler.py", files["handler.py"].replace('get("text")', 'get("value")'))
    registry.activate_extension(name)
    assert before.validate_arguments(name + "_echo", {"text": "old"}) is None
    result = await registry.dispatch(
        name + "_echo", {"text": "old"}, bound_skill=before.binding_for(name + "_echo"),
    )
    assert result.success and result.output == "old"
    after = before.refresh_extensions()
    assert after.validate_arguments(name + "_echo", {"text": "old"}) is not None
    assert after.validate_arguments(name + "_echo", {"value": "new"}) is None
    result = await registry.dispatch(
        name + "_echo", {"value": "new"}, bound_skill=after.binding_for(name + "_echo"),
    )
    assert result.success and result.output == "new"


@pytest.mark.asyncio
async def test_removed_tool_finishes_its_round_but_new_name_requires_request():
    from mochi.tool_availability import ToolAvailability

    name = "local_renamed"
    files = template.files(name)
    store.scaffold(name)
    registry.activate_extension(name)
    before = ToolAvailability.from_definitions(registry.get_tools_by_names([name]), source="test")
    store.write_file(name, "SKILL.md", files["SKILL.md"].replace(name + "_echo", name + "_new"))
    store.write_file(name, "handler.py", files["handler.py"].replace(name + "_echo", name + "_new"))
    registry.activate_extension(name)
    old = await registry.dispatch(
        name + "_echo", {"text": "old"}, bound_skill=before.binding_for(name + "_echo"),
    )
    assert old.success and old.output == "old"
    assert registry.get_tool_skill(name + "_echo") is None
    assert registry.get_tool_skill(name + "_new") == name
    assert before.refresh_extensions().names == frozenset()


def test_disabled_or_unconfigured_activation_never_imports_candidate():
    name = "local_unready"
    files = template.files(name)
    metadata = files["SKILL.md"].replace(
        "type: tool",
        "type: tool\nrequires_config: [ACCESS_TOKEN]\nconfig:\n"
        "  ACCESS_TOKEN:\n    type: str\n    default: \"\"\n    secret: true",
    )
    store.scaffold(name, skill_md=metadata, handler_py="raise AssertionError('must not import')\n")
    set_skill_enabled(name, False)
    with pytest.raises(store.ExtensionError) as disabled:
        registry.activate_extension(name)
    assert disabled.value.code == "extension_disabled"
    assert not disabled.value.state_change_unknown
    set_skill_enabled(name, True)
    with pytest.raises(store.ExtensionError) as missing:
        registry.activate_extension(name)
    assert missing.value.code == "missing_config"
    assert not missing.value.state_change_unknown
    assert not (store.extension_root(name) / "current").exists()


@pytest.mark.parametrize("via_admin", [False, True])
def test_draft_config_can_unblock_update_without_replacing_live_schema(monkeypatch, via_admin):
    name = "local_config_update"
    metadata = template.files(name)["SKILL.md"].replace(
        "type: tool", "type: tool\nconfig:\n  COUNT:\n    type: int\n    default: 1",
    )
    store.scaffold(name, skill_md=metadata)
    registry.activate_extension(name)
    old = registry.get_skill(name)
    updated = metadata.replace(
        "config:", 'requires_config: [ACCESS_TOKEN]\nconfig:\n'
        '  ACCESS_TOKEN:\n    type: str\n    default: ""\n    secret: true',
    )
    store.write_file(name, "SKILL.md", updated)
    with pytest.raises(store.ExtensionError) as missing:
        registry.activate_extension(name)
    assert missing.value.code == "missing_config"
    client = _client(monkeypatch)
    info = next(s for s in client.get("/api/skills").json()["skills"] if s["name"] == name)
    assert info["loaded"] and info["enabled"]
    assert info["config_missing"] == []
    assert info["requires_config"] == []
    assert "ACCESS_TOKEN" in info["config_required"]
    assert any(f["key"] == "ACCESS_TOKEN" for f in info["config_schema"])
    management = SkillManagementSkill()
    assert "ACCESS_TOKEN" in management._get_skill_config(name).output
    for key, value in (("ACCESS_TOKEN", "test-new-credential"), ("COUNT", "7")):
        if via_admin:
            response = client.put(f"/api/skills/{name}/config", json={"key": key, "value": value})
            assert response.status_code == 200
            assert "test-new-credential" not in response.text
        else:
            response = management._set_skill_config(name, key, value)
            assert response.success
            assert "test-new-credential" not in response.output
    assert registry.get_skill(name) is old
    assert old.config["COUNT"] == 7
    assert "ACCESS_TOKEN" not in old.config
    assert "test-new-credential" not in management._get_skill_config(name).output
    registry.activate_extension(name)
    assert registry.get_skill(name) is not old
    assert registry.get_skill(name).config["ACCESS_TOKEN"] == "test-new-credential"
    assert registry.get_skill(name).config["COUNT"] == 7


def test_admin_enable_failure_reports_saved_flag_and_real_error(monkeypatch):
    name = "local_enable_failure"
    store.scaffold(name, handler_py="raise RuntimeError('cannot load this tool')\n")
    store.publish(name, store.extension_root(name) / "draft")
    set_skill_enabled(name, False)
    response = _client(monkeypatch).put(f"/api/skills/{name}/enabled", json={"enabled": True})
    assert response.status_code == 409
    assert not response.json()["ok"]
    assert response.json()["enabled"]
    assert not response.json()["loaded"]
    assert "cannot load this tool" in response.json()["load_error"]
    assert name not in get_disabled_skills()


@pytest.mark.asyncio
async def test_developer_inspects_multiple_files_and_create_accepts_all_source():
    registry.discover()
    name = "local_authored"
    files = template.files(name)
    authored_handler = files["handler.py"] + "\n# Main-authored helper\n"
    result = await registry.dispatch(
        "edit_workspace",
        {"action": "create", "path": f"extensions/{name}/draft", "files": [
            {"path": "SKILL.md", "content": files["SKILL.md"]},
            {"path": "handler.py", "content": authored_handler},
            {"path": "smoke.py", "content": files["smoke.py"]},
        ]},
        actor="main", user_id=1,
    )
    assert result.success
    assert (store.extension_root(name) / "draft" / "handler.py").read_text(
        encoding="utf-8",
    ) == authored_handler
    read = await registry.dispatch(
        "browse_workspace",
        {"action": "read", "paths": [
            f"extensions/{name}/draft/SKILL.md", f"extensions/{name}/draft/handler.py",
        ]},
        actor="main",
    )
    assert read.success
    assert "Main-authored helper" in read.output
    denied = await registry.dispatch(
        "edit_workspace", {"action": "create", "path": "extensions/local_denied/draft"},
        actor="lite",
    )
    assert not denied.success
    assert not store.extension_root("local_denied").exists()


def test_agent_bridge_uses_real_competing_catalog_with_isolated_storage(tmp_path, monkeypatch):
    """A fresh process tests isolation without scripted provider/model replies."""
    output = tmp_path / "bridge"
    output.mkdir()
    monkeypatch.setenv("MOCHI_BRIDGE_AMBIENT_SENTINEL", "must-not-reach-runtime")
    probe = r'''
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
repository = Path.cwd()
host_data = repository / "data"

def forbid_host_data(event, args):
    if event not in {"open", "os.listdir", "os.scandir"}:
        return
    value = args[0]
    if not isinstance(value, (str, bytes, os.PathLike)):
        return
    path = Path(os.fsdecode(value)).absolute()
    if path == repository / ".env" or path.is_relative_to(host_data):
        raise AssertionError("Bridge attempted host data access")

sys.addaudithook(forbid_host_data)
from tests.e2e.agent_bridge import Bridge, _prepare_runtime

output = Path(sys.argv[1])
bridge = Bridge(output, "Check catalog mechanics", 30, 60)
with ExitStack() as stack:
    _, config = _prepare_runtime(stack, bridge)
    import mochi.db as db
    import mochi.core_store as core
    import mochi.diary as diary
    import mochi.heartbeat as heartbeat
    import mochi.mochi_files_store as documents
    import mochi.observers as observers
    import mochi.reminder_timer as reminders
    import mochi.skills as registry
    import mochi.tool_policy as policy
    from mochi.extensions import store

    runtime = output / "runtime"
    assert not os.getenv("MOCHI_BRIDGE_AMBIENT_SENTINEL")
    assert not os.getenv("MAIN_API_KEY")
    assert not os.getenv("TELEGRAM_BOT_TOKEN")
    assert db.DB_PATH == config.DB_PATH == runtime / "mochi.db"
    assert core.DATA_DIR == runtime / "core_data"
    assert documents.DATA_DIR == runtime / "files_data"
    assert store.ROOT == runtime / "extensions"
    assert diary.diary.path == runtime / "diary.md"
    assert diary._DATA_DIR == runtime
    assert heartbeat._STATE_FILE == runtime / ".heartbeat_state"
    assert not observers._observers
    assert reminders._send_callback is None and reminders._heap_event is None
    assert heartbeat._runtime_prepare_callback is None
    assert "mochi.admin.admin_server" not in sys.modules
    assert "development" not in db.get_disabled_skills()
    discovery = bridge.evidence["discovery"]
    assert discovery["missing_builtin_skills"] == []
    expected = {
        "personal_workspace", "workspace", "memory", "habit", "todo",
        "web_search", "meal", "reminder", "skill_management",
    }
    assert expected <= set(discovery["registered_skills"])
    names = {
        item["function"]["name"]
        for item in policy.filter_tools(registry.get_tools(transport="agent_bridge"))
    }
    assert {
        "browse_workspace", "edit_workspace", "run_extension", "activate_extension",
        "write_diary", "read_diary", "update_core", "recall_memory", "edit_habit",
        "query_habit", "manage_todo", "web_search", "list_skills",
    } <= names
    assert "get_weather" not in names
    assert discovery["missing_config"]["weather"] == ["WEATHER_CITY"]
    assert registry.get_skill("development") is None
    assert registry.get_skill("mochi_files") is None

    async def exercise_storage():
        for tool, args in [
            ("write_diary", {"entry": "Bridge isolation test entry"}),
            ("manage_todo", {"action": "add", "task": "Bridge isolation test task"}),
            ("edit_workspace", {
                "action": "create", "path": "documents/probe.md",
                "content": "Bridge isolation test document",
            }),
            ("recall_memory", {"query": "Bridge isolation test"}),
        ]:
            result = await registry.dispatch(tool, args, actor="main", user_id=1)
            assert result.success, (tool, result.error_code, result.output)

    asyncio.run(exercise_storage())
    assert (runtime / "diary.md").is_file()
    assert (documents.DATA_DIR / documents.ACTIVE_DIRNAME / "probe.md").is_file()
    print(json.dumps({"registered": len(discovery["registered_skills"]), "isolated": True}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(output)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["isolated"]
    assert result["registered"] >= 14
