"""Host-contract regressions against authored, never template-derived packages."""

import ast
import asyncio
from dataclasses import asdict, fields
import inspect
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mochi import db, mochi_files_store, tool_policy
from mochi.extensions import loader, runner, store
from mochi.mod_api import v1
import mochi.skills as registry
from mochi.skills import base
from mochi.skills.personal_workspace.handler import PersonalWorkspaceSkill


FIXTURES = Path(__file__).parent / "fixtures" / "mod_contract"
PACKAGES = [("legacy", "local_legacy"), ("native_v1", "local_native")]
LEGACY = {"declared": None, "effective": 1, "status": "legacy"}
SUPPORTED = {"declared": 1, "effective": 1, "status": "supported"}
UNSUPPORTED = {"declared": 2, "effective": 2, "status": "unsupported"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Use isolated storage and only the real workspace plus fixture tools."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "contract.db")
    monkeypatch.setattr(mochi_files_store, "DATA_DIR", tmp_path / "documents")
    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    monkeypatch.setattr(store, "_ERRORS", {})
    monkeypatch.setattr(tool_policy, "_deny_set", set())
    monkeypatch.setattr(tool_policy, "_call_log", {})
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setattr(registry, "_SKILLS_DIR", bundled)
    for name in ("_skills", "_tool_map", "_prompt_hooks", "_external_errors", "_capability_summary"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_external_discovered", False)
    db.init_db()
    registry._register_skill(PersonalWorkspaceSkill())


@pytest.fixture(autouse=True)
def mock_config():
    """No production discovery or configuration changes are needed."""


def copy_fixture(fixture, name, area="draft"):
    destination = store.extension_root(name) / area
    shutil.copytree(FIXTURES / fixture, destination)
    return destination


def source_bytes(package):
    return {
        str(path.relative_to(package)): path.read_bytes()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


def call(tool, **args):
    return asyncio.run(registry.dispatch(tool, args, actor="main", user_id=42))


def output(result):
    assert result.success, result.output
    return json.loads(result.output)


def assert_public_fields(value, expected):
    actual = asdict(value)
    assert {key: actual[key] for key in expected} == expected


def info(name):
    return next(item for item in registry.get_skill_info_all() if item["name"] == name)


def listed(name):
    return output(call("browse_workspace", action="list", path=f"extensions/{name}"))["packages"][0]


def declare(package, declaration):
    metadata = package / "SKILL.md"
    text = metadata.read_text(encoding="utf-8")
    metadata.write_text(text.replace("type: tool\n", f"type: tool\n{declaration}\n"), encoding="utf-8")


def import_markers(package, directory):
    markers = []
    for filename in ("__init__.py", "handler.py"):
        marker = directory / (filename + ".imported")
        (package / filename).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
            encoding="utf-8",
        )
        markers.append(marker)
    return markers


def restart_registry():
    for name in ("_skills", "_tool_map", "_prompt_hooks", "_external_errors", "_capability_summary"):
        getattr(registry, name).clear()
    registry._external_discovered = False
    registry._register_skill(PersonalWorkspaceSkill())
    return registry.discover()


def test_public_exports_are_existing_classes_and_import_is_runtime_light():
    assert (v1.Skill, v1.SkillContext, v1.SkillResult) == (
        base.Skill, base.SkillContext, base.SkillResult,
    )
    code = """import sys
from mochi.mod_api.v1 import Skill, SkillContext, SkillResult, run_candidate
forbidden = {
    'mochi.db', 'mochi.config', 'mochi.runtime', 'mochi.main', 'mochi.main_runtime',
    'mochi.ai_client', 'mochi.diary', 'mochi.transport', 'mochi.skill_config_resolver',
    'mochi.extensions.worker',
}
assert not forbidden & sys.modules.keys(), forbidden & sys.modules.keys()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_context_and_result_constructor_contract_and_independent_defaults():
    context_defaults = {
        "trigger": "script", "user_id": 0, "channel_id": 0, "transport": "",
        "actor": "", "owner_authorized": False, "tool_name": "", "args": {},
        "observation": None,
    }
    result_defaults = {
        "output": "", "actions": [], "success": True, "summary": "",
        "entity_refs": [], "state_changed": False, "error_code": "",
        "retryable": None, "execution_started": False, "state_change_unknown": False,
        "content_source": "",
    }
    assert set(context_defaults) <= {field.name for field in fields(v1.SkillContext)}
    assert set(result_defaults) <= {field.name for field in fields(v1.SkillResult)}
    assert inspect.signature(v1.SkillContext).parameters["trigger"].default is inspect.Parameter.empty
    assert_public_fields(v1.SkillContext("script"), context_defaults)
    assert_public_fields(v1.SkillResult(), result_defaults)
    context = v1.SkillContext(
        "tool_call", 42, 7, "telegram", "main", True, "local_native_record",
        {"text": "positional"}, {"facts": []},
    )
    assert_public_fields(context, {
        "trigger": "tool_call", "user_id": 42, "channel_id": 7, "transport": "telegram",
        "actor": "main", "owner_authorized": True, "tool_name": "local_native_record",
        "args": {"text": "positional"}, "observation": {"facts": []},
    })
    result = v1.SkillResult("text", [], False, "receipt", ["counter:1"], True, "error", False, True, True, "source")
    assert_public_fields(result, dict(zip(result_defaults, [
        "text", [], False, "receipt", ["counter:1"], True, "error", False, True, True, "source",
    ])))
    v1.SkillContext("script").args["not_shared"] = True
    result.actions.append({"type": "message"})
    result.entity_refs.append("counter:2")
    assert_public_fields(v1.SkillContext("script"), context_defaults)
    assert_public_fields(v1.SkillResult(), result_defaults)


@pytest.mark.parametrize("fixture,name", PACKAGES)
def test_authored_packages_activate_execute_and_survive_fresh_registry(fixture, name):
    draft = copy_fixture(fixture, name)
    original = source_bytes(FIXTURES / fixture)
    if fixture == "legacy":
        assert b"mod_api" not in original["SKILL.md"]
        assert b"from mochi.skills.base import" in original["handler.py"]
    for key, value in {"LABEL": "saved-label", "STEP": "4", "ACCESS_TOKEN": "saved-secret"}.items():
        db.set_skill_config(name, key, value)
    db.set_skill_enabled(name, True)
    saved_config = db.get_skill_config(name)
    root = draft.parent
    (root / "data").mkdir()
    counter = root / "data" / "counter.json"
    counter.write_text('{"count": 10}', encoding="utf-8")
    (root / "data" / "owner-file.bin").write_bytes(b"\x00unchanged\xff")
    expected_api = LEGACY if fixture == "legacy" else SUPPORTED
    assert info(name)["mod_api"] == {"draft": expected_api, "active": None}

    receipt = output(call("activate_extension", path=f"extensions/{name}/draft"))
    assert receipt["active"] and receipt["persisted"]
    skill = registry.get_skill(name)
    assert isinstance(skill, v1.Skill)
    assert skill.constructor_token == "saved-secret"
    assert skill.constructor_step == 4 and skill.constructor_data_exists
    assert skill.data_dir == root / "data"
    tool = f"{name}_record"
    assert receipt["tools"] == [tool]
    assert registry.skill_for_tool(tool) == name
    before_tools = skill.get_tools()
    assert before_tools[0]["_load"] == "on_demand"
    properties = before_tools[0]["function"]["parameters"]["properties"]
    assert properties["text"]["type"] == "string"
    assert properties["mode"]["enum"] == ["record", "reject", "crash"]

    result = call(tool, text="first")
    assert output(result) == {"count": 14, "text": "saved-label: first", "actor": "main", "user_id": 42}
    assert result.execution_started and result.state_changed and not result.state_change_unknown
    assert result.summary == "Saved one counter record."
    assert result.entity_refs == ["counter:14"]
    assert result.actions == [{"type": "message", "content": "saved-label: first"}]
    assert result.content_source == "agent_authored_document"
    assert result.error_code == "" and result.retryable is None
    persisted_data = counter.read_bytes()
    assert name in restart_registry()
    restarted = registry.get_skill(name)
    assert restarted is not skill
    assert restarted.get_tools() == before_tools
    assert restarted.data_dir == root / "data"
    assert counter.read_bytes() == persisted_data
    assert db.get_skill_config(name) == saved_config
    assert name not in db.get_disabled_skills()
    for package in (draft, root / "current", FIXTURES / fixture):
        assert source_bytes(package) == original
    assert (root / "data" / "owner-file.bin").read_bytes() == b"\x00unchanged\xff"
    for view in (info(name), listed(name)):
        assert view["loaded"] and view["enabled"] and not view["load_error"]
        assert view["mod_api"] == {"draft": expected_api, "current": expected_api, "active": expected_api}
    assert output(call(tool, text="after restart"))["count"] == 18


@pytest.mark.parametrize("fixture,name", PACKAGES)
def test_handler_results_and_runtime_exception_facts(fixture, name):
    draft = copy_fixture(fixture, name)
    skill = v1.run_candidate(name, draft, draft.parent / "sample-data")
    assert skill.constructor_step == 2 and skill.constructor_token == ""
    context = v1.SkillContext("script", tool_name=f"{name}_record", args={"text": "test"})
    authored = asyncio.run(skill.execute(context))
    assert authored.success and authored.state_changed
    assert not authored.execution_started
    context.args["mode"] = "reject"
    failure = asyncio.run(skill.run(context))
    assert_public_fields(failure, {
        "output": "Record rejected", "success": False, "actions": [],
        "summary": "No record saved.", "entity_refs": [], "state_changed": False,
        "error_code": "record_rejected", "retryable": True,
        "execution_started": True, "state_change_unknown": False, "content_source": "",
    })
    context.args["mode"] = "crash"
    crash = asyncio.run(skill.run(context))
    assert not crash.success and crash.execution_started and crash.state_change_unknown
    assert crash.error_code == "skill_exception" and crash.retryable is False
    assert not crash.state_changed and crash.summary == "" and crash.entity_refs == []
    assert "Interrupted after saving" in crash.output
    assert json.loads((skill.data_dir / "counter.json").read_text())["count"] == 4
    unknown = call(f"{name}_not_registered")
    assert not unknown.success and not unknown.execution_started
    assert unknown.error_code == "unknown_tool"


@pytest.mark.parametrize("fixture,name", PACKAGES)
def test_metadata_keeps_existing_parser_shape_and_equality(fixture, name):
    draft = copy_fixture(fixture, name)
    parsed = base._parse_skill_md(str(draft / "SKILL.md"))
    assert loader.read_metadata(name, draft) == parsed
    assert loader.validate_package(name, draft) == parsed
    assert set(parsed) == {
        "meta", "tools", "triggers", "capability_context", "type", "multi_turn",
        "requires_config", "requires_env", "has_sense", "locked", "diary",
        "diary_status_order", "config_schema", "sub_skills", "exclude_transports",
    }


@pytest.mark.parametrize("declaration,expected", [
    ("", LEGACY),
    ("mod_api: 1", SUPPORTED),
    ("mod_api: 2", UNSUPPORTED),
    ("mod_api: 123", {"declared": 123, "effective": 123, "status": "unsupported"}),
    *[(f"mod_api: {value}", {"declared": None, "effective": None, "status": "invalid"})
      for value in ("", "0", "-1", "true", "1.0", '"1"', "[1]")],
    ("mod_api: 1\nmod_api: 1", {"declared": None, "effective": None, "status": "invalid"}),
    ("mod_api: 1\nmod_api: 2", {"declared": None, "effective": None, "status": "invalid"}),
    pytest.param(" mod_api: 2", UNSUPPORTED, id="indented-unsupported"),
    pytest.param(" mod_api: false", {"declared": None, "effective": None, "status": "invalid"},
                 id="indented-false"),
    pytest.param("mod_api: 1\n mod_api: 1",
                 {"declared": None, "effective": None, "status": "invalid"},
                 id="indented-duplicate"),
])
def test_inspection_normalizes_version_without_imports(declaration, expected, tmp_path):
    draft = copy_fixture("legacy", "local_legacy")
    declare(draft, declaration)
    markers = import_markers(draft, tmp_path)
    actual = loader.inspect_mod_api(draft)
    assert {key: actual[key] for key in expected} == expected
    assert not any(path.exists() for path in markers)


@pytest.mark.parametrize("fixture,name", PACKAGES)
def test_nested_config_field_named_mod_api_is_not_a_version_declaration(fixture, name):
    draft = copy_fixture(fixture, name)
    metadata = draft / "SKILL.md"
    metadata.write_text(metadata.read_text(encoding="utf-8").replace(
        "    secret: true\n",
        "    secret: true\n"
        "  mod_api:\n"
        "    type: int\n"
        "    default: 2\n",
    ), encoding="utf-8")
    original = source_bytes(draft)
    assert loader.inspect_mod_api(draft) == (LEGACY if fixture == "legacy" else SUPPORTED)
    parsed = loader.read_metadata(name, draft)
    assert parsed == base._parse_skill_md(str(metadata))
    assert next(field for field in parsed["config_schema"] if field.key == "mod_api").default == "2"
    skill = v1.run_candidate(name, draft, draft.parent / "sample-data")
    assert skill.config["mod_api"] == 2
    result = asyncio.run(skill.run(v1.SkillContext("script", args={"text": "nested field"})))
    assert output(result)["count"] == 2
    assert source_bytes(draft) == original


def test_missing_metadata_keeps_read_error_and_management_reports_unavailable(tmp_path):
    package = store.extension_root("local_missing") / "current"
    package.mkdir(parents=True)
    markers = import_markers(package, tmp_path)
    with pytest.raises(store.ExtensionError) as caught:
        loader.inspect_mod_api(package)
    assert caught.value.code == "not_found"
    for view in (info("local_missing"), listed("local_missing")):
        assert not view["loaded"] and view["activation_required"]
        assert view["load_error"]
        assert view["mod_api"]["active"] is None
        metadata = view["mod_api"]["current"]
        assert metadata["declared"] is None and metadata["effective"] is None
        assert metadata["status"] == "unavailable"
        assert metadata["error"] == str(caught.value)
    assert registry.get_skill("local_missing") is None
    assert not any(path.exists() for path in markers)


@pytest.mark.parametrize("declaration,error", [
    ("mod_api: 2", "unsupported_mod_api"),
    ("mod_api: 0", "invalid_mod_api"),
    ("mod_api: 1\nmod_api: 1", "invalid_mod_api"),
    pytest.param(" mod_api: 2", "unsupported_mod_api", id="indented-unsupported"),
    pytest.param(" mod_api: false", "invalid_mod_api", id="indented-false"),
    pytest.param("mod_api: 1\n mod_api: 1", "invalid_mod_api", id="indented-duplicate"),
])
@pytest.mark.parametrize("entry", ["activation", "startup", "enable", "candidate"])
def test_incompatible_packages_rejected_before_package_or_handler_import(
    declaration, error, entry, tmp_path,
):
    name = "local_legacy"
    package = copy_fixture("legacy", name, "current" if entry in {"startup", "enable"} else "draft")
    declare(package, declaration)
    markers = import_markers(package, tmp_path)
    original = source_bytes(package)
    data = package.parent / "data"
    data.mkdir()
    (data / "keep").write_bytes(b"private-data")
    if entry == "startup":
        copy_fixture("native_v1", "local_native", "current")
        assert "local_native" in registry.discover()
        assert output(call("local_native_record", text="unrelated"))["count"] == 2
        assert registry.get_skill(name) is None
        for view in (info(name), listed(name)):
            assert not view["loaded"] and view["activation_required"]
            assert view["load_error"]
            assert view["mod_api"]["active"] is None
            assert view["mod_api"]["current"]["status"] == (
                "unsupported" if error == "unsupported_mod_api" else "invalid"
            )
        text = output(call(
            "browse_workspace", action="read",
            path=f"extensions/{name}/current/SKILL.md",
        ))["files"][0]["content"]
        assert declaration in text.replace("\r\n", "\n")
        db.set_skill_enabled(name, False)
        assert info(name)["admin_disabled"]
    else:
        with pytest.raises(store.ExtensionError) as caught:
            if entry == "activation":
                registry.activate_extension(name)
            elif entry == "enable":
                registry.load_installed_extension(name)
            else:
                v1.run_candidate(name, package, data)
        assert caught.value.code == error
        if error == "unsupported_mod_api":
            assert "2" in str(caught.value) and "1" in str(caught.value)
    assert not any(path.exists() for path in markers)
    assert source_bytes(package) == original
    assert (data / "keep").read_bytes() == b"private-data"
    assert registry.get_skill(name) is None


@pytest.mark.parametrize("declaration,error,status", [
    ("mod_api: 2", "unsupported_mod_api", "unsupported"),
    ("mod_api: false", "invalid_mod_api", "invalid"),
])
def test_incompatible_draft_does_not_mislabel_or_replace_active_snapshot(
    declaration, error, status, tmp_path,
):
    draft = copy_fixture("legacy", "local_legacy")
    output(call("activate_extension", path="extensions/local_legacy/draft"))
    active = registry.get_skill("local_legacy")
    current = source_bytes(draft.parent / "current")
    declare(draft, declaration)
    markers = import_markers(draft, tmp_path)
    failure = call("activate_extension", path="extensions/local_legacy/draft")
    assert not failure.success and failure.error_code == error
    assert not failure.state_changed and not failure.state_change_unknown
    assert registry.get_skill("local_legacy") is active
    assert source_bytes(draft.parent / "current") == current
    for view in (info("local_legacy"), listed("local_legacy")):
        assert view["loaded"] and view["enabled"] and not view["activation_required"]
        assert view["mod_api"]["draft"]["status"] == status
        assert view["mod_api"]["current"] == LEGACY
        assert view["mod_api"]["active"] == LEGACY
    assert output(call("local_legacy_record", text="still works"))["count"] == 2
    output(call("edit_workspace", action="append",
                path="extensions/local_legacy/draft/handler.py", content="\n# still editable\n"))
    # An out-of-band disk edit must not change the loaded snapshot's declaration.
    declare(draft.parent / "current", "mod_api: 2")
    for view in (info("local_legacy"), listed("local_legacy")):
        assert view["mod_api"]["current"] == UNSUPPORTED
        assert view["mod_api"]["active"] == LEGACY and view["loaded"]
    assert output(call("local_legacy_record", text="pinned"))["count"] == 4
    assert not any(path.exists() for path in markers)


def test_public_candidate_helper_uses_only_defaults_and_explicit_sample_config(monkeypatch):
    draft = copy_fixture("native_v1", "local_native")
    db.set_skill_config("local_native", "ACCESS_TOKEN", "live-db-secret")
    monkeypatch.setenv("ACCESS_TOKEN", "live-env-secret")
    monkeypatch.setenv("SKILL_LOCAL_NATIVE_ACCESS_TOKEN", "live-scoped-secret")

    def forbidden(*args, **kwargs):
        raise AssertionError("Candidate helper read live configuration")

    monkeypatch.setattr(db, "get_skill_config", forbidden)
    defaults = v1.run_candidate("local_native", draft, draft.parent / "default-data")
    assert defaults.config == {"LABEL": "native", "STEP": 2, "ACCESS_TOKEN": ""}
    sample = {"STEP": 7, "ACCESS_TOKEN": "sample-token"}
    skill = v1.run_candidate("local_native", draft, draft.parent / "sample-data", config=sample)
    sample["ACCESS_TOKEN"] = "mutated"
    assert skill.config == {"LABEL": "native", "STEP": 7, "ACCESS_TOKEN": "sample-token"}
    assert skill.constructor_token == "sample-token" and skill.constructor_step == 7
    assert skill.get_config("MISSING") == ""
    result = asyncio.run(skill.run(v1.SkillContext("script", args={"text": "sample"})))
    assert output(result)["count"] == 7
    assert registry.get_skill("local_native") is None
    assert not (draft.parent / "data").exists() and not (draft.parent / "current").exists()
    report = asyncio.run(runner.run_extension("local_native"))
    assert report["success"], report
    assert json.loads(report["stdout"])["text"] == "sample: smoke"
    assert "live-db-secret" not in report["stdout"] + report["stderr"]


def test_restart_preserves_disabled_package_source_configuration_and_data():
    current = copy_fixture("legacy", "local_legacy", "current")
    original = source_bytes(current)
    db.set_skill_enabled("local_legacy", False)
    db.set_skill_config("local_legacy", "LABEL", "saved-disabled-label")
    data = current.parent / "data"
    data.mkdir()
    (data / "keep").write_bytes(b"owner data")
    assert "local_legacy" not in restart_registry()
    assert source_bytes(current) == original
    assert db.get_skill_config("local_legacy") == {"LABEL": "saved-disabled-label"}
    assert (data / "keep").read_bytes() == b"owner data"
    state = info("local_legacy")
    assert state["admin_disabled"] and not state["loaded"]
    assert state["mod_api"] == {"current": LEGACY, "active": None}
    db.set_skill_enabled("local_legacy", True)
    assert registry.load_installed_extension("local_legacy")
    assert output(call("local_legacy_record", text="enabled"))["text"] == "saved-disabled-label: enabled"


def test_explicit_draft_script_is_not_blanket_gated_by_version(tmp_path):
    draft = copy_fixture("legacy", "local_legacy")
    declare(draft, "mod_api: 2")
    markers = import_markers(draft, tmp_path)
    (draft / "inspect.py").write_text("print('explicit draft script ran')\n", encoding="utf-8")
    report = output(call(
        "run_extension", path="extensions/local_legacy/draft", script="inspect.py",
    ))
    assert report["success"] and report["stdout"].strip() == "explicit draft script ran"
    assert registry.get_skill("local_legacy") is None
    assert not any(path.exists() for path in markers)


def test_guide_and_template_use_public_v1_and_existing_discovery():
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    guide = output(call("browse_workspace", action="guide", path="extensions/local_guided/draft"))
    assert guide["supported_mod_apis"] == [1] and guide["development_enabled"]
    assert "mochi.mod_api.v1" in guide["guide"]
    assert "## Personal extension API v1" in guide["guide"]
    assert "Tool Mod contract" not in guide["guide"]
    template = guide["template"]
    assert template["SKILL.md"].count("mod_api: 1") == 1
    for filename in ("handler.py", "smoke.py"):
        modules = {
            node.module for node in ast.walk(ast.parse(template[filename]))
            if isinstance(node, ast.ImportFrom) and node.module.startswith("mochi")
        }
        assert modules == {"mochi.mod_api.v1"}
    assert "run_candidate" in template["smoke.py"]
    assert not store.ROOT.exists()
    output(call("edit_workspace", action="create", path=guide["draft_path"], files=[
        {"path": name, "content": content} for name, content in template.items()
    ]))
    assert output(call("run_extension", path=guide["draft_path"]))["success"]
    output(call("activate_extension", path=guide["draft_path"]))
    receipt, definitions = resolve_request({"skills": ["local_guided"]}, ToolAvailability())
    assert receipt["ok"]
    assert [item["function"]["name"] for item in definitions] == ["local_guided_echo"]
    result = call("local_guided_echo", text="native v1 works")
    assert result.success and result.output == "native v1 works" and result.execution_started
