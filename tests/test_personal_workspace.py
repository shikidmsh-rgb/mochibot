"""Personal workspace handlers exercise isolated existing document/source storage."""

import asyncio
import json
from pathlib import Path

import pytest

from mochi import db, mochi_files_store as documents, personal_workspace as workspace
from mochi.extensions import runner, store
import mochi.skills as registry
from mochi.skills.base import SkillContext
from mochi.skills.personal_workspace.handler import PersonalWorkspaceSkill


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from mochi import tool_policy

    monkeypatch.setattr(tool_policy, "_deny_set", set())
    monkeypatch.setattr(tool_policy, "_call_log", {})
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(documents, "DATA_DIR", tmp_path / "documents_data")
    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    monkeypatch.setattr(store, "_ERRORS", {})
    monkeypatch.setattr(registry, "_skills", {})
    monkeypatch.setattr(registry, "_tool_map", {})
    monkeypatch.setattr(registry, "_external_discovered", True)
    monkeypatch.setattr(registry, "_external_errors", {})
    monkeypatch.setattr(registry, "_capability_summary", {})
    db.init_db()


@pytest.fixture(autouse=True)
def mock_config():
    """No runtime configuration or production skill discovery is needed."""


@pytest.fixture
def skill():
    return PersonalWorkspaceSkill()


def call(skill, tool="browse_workspace", *, actor="main", trigger="tool_call", args=None, **kwargs):
    return asyncio.run(skill.execute(SkillContext(
        trigger=trigger, actor=actor, tool_name=tool, args=kwargs if args is None else args,
    )))


def output(result):
    assert result.success, result.output
    return json.loads(result.output)


def create_package(skill, name="local_workspace", **extra):
    path = f"extensions/{name}/draft"
    guide = output(call(skill, action="guide", path=path))
    files = {**guide["template"], **extra}
    receipt = call(skill, "edit_workspace", action="create", path=path, files=[
        {"path": key, "content": value} for key, value in files.items()
    ])
    assert output(receipt)["draft_path"] == path
    assert receipt.state_changed
    return path


def test_document_create_append_exact_edit_previous_and_persistence(skill):
    path = "documents/reading/list.md"
    body = "# Reading\nremember:47 memory:84\nFirst"
    saved = call(skill, "edit_workspace", action="create", path=path, content=body)
    assert saved.state_changed and output(saved)["bytes"] == len(body.encode())
    assert body not in saved.summary and not saved.entity_refs
    conflict = call(skill, "edit_workspace", action="create", path=path, content="overwrite")
    assert not conflict.success and conflict.error_code == "conflict" and not conflict.state_changed
    read = call(PersonalWorkspaceSkill(), action="read", path=path)
    assert output(read)["files"][0]["content"] == body
    assert read.content_source == "agent_authored_document" and not read.entity_refs
    assert "memory:84" not in read.summary
    output(call(skill, "edit_workspace", action="append", path=path, content="\nSecond"))
    previous = documents.DATA_DIR / documents.PREVIOUS_DIRNAME / "reading" / "list.md"
    assert previous.read_text(encoding="utf-8") == body
    output(call(skill, "edit_workspace", action="edit", path=path, old_text="First", new_text="One"))
    assert previous.read_text(encoding="utf-8") == body + "\nSecond"
    assert output(call(skill, action="read", path=path))["files"][0]["content"] == body.replace("First", "One") + "\nSecond"
    stale = call(skill, "edit_workspace", action="edit", path=path, old_text="absent", new_text="")
    assert stale.error_code == "conflict" and not stale.state_changed
    listing = output(call(skill, action="list", path="documents"))
    assert [entry["path"] for entry in listing["files"]] == [path]
    for action in ("replace", "remove"):
        result = call(skill, "edit_workspace", action=action, path=path, **({"content": ""} if action == "replace" else {}))
        assert not result.success and not result.state_changed


def test_guide_named_template_complete_create_run_activate_use_and_state(skill):
    default = output(call(skill, action="guide"))
    assert default["draft_path"] == "extensions/local_example/draft"
    assert "SKILL.md" in default["template"] and "handler.py" in default["template"]
    assert "smoke.py" in default["template"] and default["guide"]
    assert not store.ROOT.exists()
    path = create_package(skill)
    tree = output(call(skill, action="list", path=path))
    assert len(tree["files"]) == 4
    report = call(skill, "run_extension", path=path)
    assert output(report)["stdout"].strip() == "Hello from local_workspace"
    assert report.state_changed and report.state_change_unknown
    assert "local_workspace" not in registry._skills
    saved_report = output(call(skill, action="report", path=path))
    assert saved_report["stdout"] == output(report)["stdout"]
    before = output(call(skill, action="list"))
    assert [area["path"] for area in before["areas"]] == ["documents", "extensions"]
    assert before["development_enabled"]
    package = before["packages"][0]
    assert package["draft_path"] == path and package["has_draft"]
    assert not package["loaded"]
    activated = call(skill, "activate_extension", path=path)
    assert output(activated)["active"] and output(activated)["persisted"] and activated.state_changed
    installed = registry._skills["local_workspace"]
    tool = installed.get_tools()[0]["function"]["name"]
    result = asyncio.run(installed.run(SkillContext(
        trigger="tool_call", actor="main", tool_name=tool, args={"text": "actual tool"},
    )))
    assert result.success and result.output == "actual tool"
    after = output(call(skill, action="list", path="extensions/local_workspace"))["packages"][0]
    assert after["loaded"] and after["enabled"] and after["installed"]
    assert after["tools"] == [tool]


def test_complete_scaffold_extra_files_and_custom_script_arguments(skill):
    path = create_package(skill, **{
        "helpers/text.txt": "helper",
        "scripts/check.py": "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
    })
    report = output(call(skill, "run_extension", path=path, script="scripts/check.py", arguments=["two words", "日本語"]))
    assert json.loads(report["stdout"]) == ["two words", "日本語"]
    assert report["script"] == "scripts/check.py"
    duplicate = call(skill, "edit_workspace", action="create", path=path, files=[])
    assert not duplicate.success and duplicate.error_code == "draft_exists"
    assert output(call(skill, action="read", path=path + "/helpers/text.txt"))["files"][0]["content"] == "helper"


def test_draft_only_exclusive_create_append_replace_and_current_previous_reads(skill):
    path = create_package(skill)
    file = path + "/notes.txt"
    output(call(skill, "edit_workspace", action="create", path=file, content="alpha"))
    output(call(skill, "activate_extension", path=path))
    for action, args in (("append", {"content": " beta"}), ("edit", {"old_text": "beta", "new_text": "gamma"})):
        receipt = output(call(skill, "edit_workspace", action=action, path=file, **args))
        assert receipt["draft_path"] == path
    assert call(skill, "edit_workspace", action="create", path=file, content="no").error_code == "already_exists"
    current = file.replace("/draft/", "/current/")
    assert output(call(skill, action="read", path=current))["files"][0]["content"] == "alpha"
    for area in ("current", "previous"):
        for action, args in (
            ("create", {"content": "x"}), ("append", {"content": "x"}),
            ("replace", {"content": "x"}), ("remove", {}),
            ("edit", {"old_text": "alpha", "new_text": "x"}),
        ):
            rejected = call(skill, "edit_workspace", action=action, path=file.replace("/draft/", f"/{area}/"), **args)
            assert not rejected.success and rejected.error_code == "invalid_path"
    output(call(skill, "activate_extension", path=path))
    assert output(call(skill, action="read", path=current))["files"][0]["content"] == "alpha gamma"
    previous = file.replace("/draft/", "/previous/")
    assert output(call(skill, action="read", path=previous))["files"][0]["content"] == "alpha"
    output(call(skill, "edit_workspace", action="replace", path=file, content="rewritten"))
    assert output(call(skill, action="read", path=current))["files"][0]["content"] == "alpha gamma"
    assert output(call(skill, "edit_workspace", action="remove", path=file))["draft_path"] == path
    assert call(skill, action="read", path=file).error_code == "not_found"


def test_disabled_development_retains_documents_inspection_and_installed_tools(skill, monkeypatch):
    path = create_package(skill)
    output(call(skill, "activate_extension", path=path))
    installed = registry._skills["local_workspace"]
    tool = installed.get_tools()[0]["function"]["name"]
    refreshes = []
    monkeypatch.setattr(registry, "refresh_capability_summary", lambda: refreshes.append(True))
    assert workspace.development_enabled()
    assert workspace.set_development_enabled(False)
    assert not workspace.set_development_enabled(False)
    assert refreshes == [True] and db.get_disabled_skills() == {"development"}
    assert len(skill.get_tools()) == 4
    assert {t["function"]["name"] for t in skill.available_tools()} == {"browse_workspace", "edit_workspace"}
    assert not output(call(skill, action="list"))["development_enabled"]
    output(call(skill, action="read", path=path + "/handler.py"))
    output(call(skill, action="list", path=path))
    assert not output(call(skill, action="guide"))["development_enabled"]
    output(call(skill, "edit_workspace", action="create", path="documents/allowed.md", content="still writable"))
    output(call(skill, "edit_workspace", action="append", path="documents/allowed.md", content="!"))
    assert output(call(skill, action="read", path="documents/allowed.md"))["files"][0]["content"] == "still writable!"
    for tool_name, args in (
        ("run_extension", {"path": path}),
        ("activate_extension", {"path": path}),
        ("edit_workspace", {"action": "append", "path": path + "/handler.py", "content": "\n"}),
        ("edit_workspace", {"action": "create", "path": "extensions/local_other/draft", "files": []}),
    ):
        result = call(skill, tool_name, **args)
        assert result.error_code == "development_disabled" and not result.state_changed
    assert registry._skills["local_workspace"] is installed
    assert asyncio.run(installed.run(SkillContext(trigger="tool_call", tool_name=tool, args={"text": "kept"}))).output == "kept"
    assert workspace.set_development_enabled(True)
    assert len(skill.available_tools()) == 4 and refreshes == [True, True]
    with pytest.raises(ValueError, match="boolean"):
        workspace.set_development_enabled("false")


@pytest.mark.parametrize("actor,trigger", [("", "tool_call"), ("observer", "tool_call"), ("main", "cron"), ("main", "script")])
def test_main_tool_calls_only(skill, actor, trigger):
    for tool in ("browse_workspace", "edit_workspace", "run_extension", "activate_extension"):
        result = call(skill, tool, actor=actor, trigger=trigger, action="list")
        assert not result.success and result.error_code == "main_only"
    assert not store.ROOT.exists()


def test_mixed_reads_share_budget_with_explicit_offsets_and_deferred_paths(skill):
    path = create_package(skill, **{"notes.txt": "日本語abc"})
    output(call(skill, "edit_workspace", action="create", path="documents/notes.md", content="12345"))
    paths = ["documents/notes.md", path + "/notes.txt"]
    mixed = output(call(skill, action="read", paths=paths, offset=1, limit=7))
    assert [item["content"] for item in mixed["files"]] == ["2345", "本語a"]
    assert [item["path"] for item in mixed["files"]] == paths
    assert [item["source_area"] for item in mixed["files"]] == ["documents", "draft"]
    assert mixed["files"][1]["next_offset"] == 4 and mixed["truncated"]
    exhausted = output(call(skill, action="read", paths=paths, limit=5))
    assert exhausted["files"][1]["deferred"] and exhausted["files"][1]["next_offset"] == 0
    assert "content" not in exhausted["files"][1]
    assert output(call(skill, action="read", path=paths[1], offset=4))["files"][0]["content"] == "bc"
    assert output(call(skill, action="read", path=paths[0], offset=500))["complete"]


def test_literal_search_bounded_source_scope_and_canonical_results(skill):
    path = create_package(skill, **{"notes.txt": "needle " * 30, "nested/more.txt": "needle"})
    private = store.ROOT / "local_workspace" / "data"
    private.mkdir()
    (private / "secret.txt").write_text("needle SECRET", encoding="utf-8")
    output(call(skill, "edit_workspace", action="create", path="documents/find.md", content="needle " * 30))
    for scope, expected in (("documents", "documents/find.md"), (path, path + "/nested/more.txt")):
        found = output(call(skill, action="search", path=scope, query="needle", limit=1))
        assert found["matches"][0]["path"] == expected
        assert found["truncated"] and found["next_offset"] == 1
        assert len(found["matches"][0]["excerpt"]) <= documents.MAX_SEARCH_EXCERPT_CHARS
        assert "SECRET" not in json.dumps(found)
    nested = output(call(skill, action="search", path=path + "/nested", query="needle"))
    assert nested["total_matches"] == 1
    remaining = output(call(skill, action="search", path=path + "/notes.txt", query="needle", offset=29, limit=1))
    assert remaining["complete"] and remaining["next_offset"] is None
    listed = output(call(skill, action="list", path=path, limit=1))
    assert listed["count"] == 1 and listed["truncated"] and listed["next_offset"] == 1
    for scope in ("extensions", "extensions/local_workspace", "extensions/local_workspace/data"):
        result = call(skill, action="search", path=scope, query="needle")
        assert not result.success and not result.state_changed
    assert not call(skill, action="search", query="needle").success


@pytest.mark.parametrize("path", [
    "/documents/note.md", r"documents\note.md", "documents/../secret.md",
    "documents/.mochi_files_previous/note.md", "documents/note.txt",
    "extensions/local_x/data/secret.txt", "extensions/local_x/_loaded/handler.py",
    "extensions/local_x/draft/../current/handler.py", "extensions/local_x/draft/CON.py",
    "extensions/local_x/draft/x.py:stream", "extensions/LOCAL_x/draft/a.py",
    "Core/core.md", "Memory/memory.md", "workspace/diary.md", "mochi/skills/base.py",
    r"C:\secret.md", "documents/a//b.md", "documents/a.md/",
])
def test_invalid_paths_fail_without_writes(skill, path):
    result = call(skill, "edit_workspace", action="create", path=path, content="no")
    assert not result.success and not result.state_changed
    assert not store.ROOT.exists()


@pytest.mark.parametrize("tool,args", [
    ("browse_workspace", []),
    ("browse_workspace", {"action": []}),
    ("browse_workspace", {"action": "read", "paths": []}),
    ("browse_workspace", {"action": "read", "paths": ["documents/a.md", 1]}),
    ("browse_workspace", {"action": "read", "path": "documents/a.md", "paths": ["documents/a.md"]}),
    ("browse_workspace", {"action": "list", "path": None}),
    ("browse_workspace", {"action": "list", "offset": True}),
    ("browse_workspace", {"action": "list", "limit": 101}),
    ("browse_workspace", {"action": "read", "path": "documents/a.md", "limit": 12001}),
    ("browse_workspace", {"action": "search", "path": "documents", "query": ""}),
    ("browse_workspace", {"action": "guide", "path": "extensions/local_x/current"}),
    ("browse_workspace", {"action": "guide", "query": "ignored?"}),
    ("edit_workspace", {"action": "create", "path": "documents/a.md"}),
    ("edit_workspace", {"action": "append", "path": "documents/a.md", "content": None}),
    ("edit_workspace", {"action": "edit", "path": "documents/a.md", "old_text": "", "new_text": "x"}),
    ("edit_workspace", {"action": "remove", "path": "extensions/local_x/draft/a.py", "content": "extra"}),
    ("run_extension", {"path": "extensions/local_x/current"}),
    ("run_extension", {"path": "extensions/local_x/draft", "arguments": None}),
    ("run_extension", {"path": "extensions/local_x/draft", "arguments": [1]}),
    ("run_extension", {"path": "extensions/local_x/draft", "arguments": ["\x00"]}),
    ("run_extension", {"path": "extensions/local_x/draft", "script": None}),
    ("run_extension", {"extension_id": "local_x"}),
    ("activate_extension", {"path": "extensions/local_x/draft", "unexpected": True}),
])
def test_malformed_arguments_return_explicit_failure(skill, tool, args):
    result = call(skill, tool, args=args)
    assert not result.success and not result.state_changed
    assert result.error_code and json.loads(result.output)["ok"] is False


@pytest.mark.parametrize("files", [
    None, "not an array", [1], [{"path": "a.py"}],
    [{"path": "a.py", "content": 1}],
    [{"path": "../escape.py", "content": ""}],
    [{"path": "a.py", "content": "", "unknown": True}],
    [{"path": "a.py", "content": ""}, {"path": "A.py", "content": ""}],
    [{"path": "a.py", "content": ""}, {"path": "a.py", "content": "again"}],
])
def test_malformed_scaffold_is_rejected_before_any_package_write(skill, files):
    result = call(skill, "edit_workspace", action="create", path="extensions/local_x/draft", files=files)
    assert not result.success and not result.state_changed
    assert not store.ROOT.exists()


def test_schemas_have_concrete_nested_files_and_stable_registration(skill):
    tools = {tool["function"]["name"]: tool["function"]["parameters"] for tool in skill.get_tools()}
    assert skill.locked
    assert set(tools) == {"browse_workspace", "edit_workspace", "run_extension", "activate_extension"}
    for schema in tools.values():
        assert schema["additionalProperties"] is False
    files = tools["edit_workspace"]["properties"]["files"]
    assert files["items"]["type"] == "object"
    assert files["items"]["required"] == ["path", "content"]
    assert files["items"]["additionalProperties"] is False
    assert files["items"]["properties"]["content"]["type"] == "string"
    files["items"]["properties"]["path"]["type"] = "number"
    assert skill.get_tools()[1]["function"]["parameters"]["properties"]["files"]["items"]["properties"]["path"]["type"] == "string"
    workspace.set_development_enabled(False)
    assert len(PersonalWorkspaceSkill().get_tools()) == 4
    assert len(PersonalWorkspaceSkill().available_tools()) == 2


@pytest.mark.parametrize("report,code,changed", [
    ({"success": False, "report_saved": True, "timed_out": True}, "extension_timeout", True),
    ({"success": False, "report_saved": True}, "extension_run_failed", True),
    ({"success": True, "report_saved": False}, "run_report_failed", False),
])
def test_run_receipts_preserve_failed_partial_effects(skill, monkeypatch, report, code, changed):
    async def run(*args, **kwargs):
        return {**report, "state_change_unknown": True, "stdout": "memory:99 authored"}

    monkeypatch.setattr(runner, "run_extension", run)
    result = call(skill, "run_extension", path="extensions/local_receipt/draft")
    assert not result.success and result.error_code == code
    assert result.state_changed == changed and result.state_change_unknown
    assert "memory:99" not in result.summary
    assert result.entity_refs == ["extension:local_receipt"]
    assert json.loads(result.output)["draft_path"] == "extensions/local_receipt/draft"


def test_real_script_failure_saved_report_and_failed_activation_preserve_current(skill):
    path = create_package(skill)
    output(call(skill, "activate_extension", path=path))
    installed = registry._skills["local_workspace"]
    output(call(skill, "edit_workspace", action="replace", path=path + "/smoke.py", content="print('authored failure')\nraise RuntimeError('expected')"))
    result = call(skill, "run_extension", path=path)
    assert result.error_code == "extension_run_failed" and result.state_changed and result.state_change_unknown
    assert "authored failure" in json.loads(result.output)["stdout"]
    assert not output(call(skill, action="report", path=path))["success"]
    output(call(skill, "edit_workspace", action="replace", path=path + "/handler.py", content="invalid syntax("))
    failed = call(skill, "activate_extension", path=path)
    assert not failed.success and not failed.state_changed
    assert registry._skills["local_workspace"] is installed
    assert "invalid syntax(" not in output(call(skill, action="read", path=path.replace("/draft", "/current") + "/handler.py"))["files"][0]["content"]


def test_activation_exception_preserves_partial_flags_and_summary(skill, monkeypatch):
    def fail(name):
        raise store.ExtensionError("publish_rollback_failed", "authored detail memory:33", state_changed=True, state_change_unknown=True)

    monkeypatch.setattr(registry, "activate_extension", fail)
    result = call(skill, "activate_extension", path="extensions/local_failed/draft")
    assert not result.success and result.state_changed and result.state_change_unknown
    assert "memory:33" not in result.summary and not result.entity_refs


def test_document_io_error_reports_possible_backup_effect(skill, monkeypatch):
    path = "documents/io.md"
    output(call(skill, "edit_workspace", action="create", path=path, content="before"))
    replace = documents._atomic_replace

    def fail_active(target, content):
        if documents.ACTIVE_DIRNAME in target.parts:
            raise OSError("active replacement failed")
        return replace(target, content)

    monkeypatch.setattr(documents, "_atomic_replace", fail_active)
    result = call(skill, "edit_workspace", action="append", path=path, content="after")
    assert not result.success and result.state_change_unknown
    assert (documents.DATA_DIR / documents.PREVIOUS_DIRNAME / "io.md").read_text() == "before"
    assert output(call(skill, action="read", path=path))["files"][0]["content"] == "before"


def test_scaffold_failure_is_atomic_and_append_obeys_file_limit(skill, monkeypatch):
    import mochi.extensions.template as template

    monkeypatch.setattr(template, "files", lambda name: {"a.txt": "seed"})
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 5)
    failed = call(skill, "edit_workspace", action="create", path="extensions/local_limit/draft", files=[
        {"path": "first.txt", "content": "okay"}, {"path": "large.txt", "content": "toolong"},
    ])
    assert not failed.success and failed.error_code == "file_too_large"
    assert not (store.ROOT / "local_limit" / "draft").exists()
    path = "extensions/local_limit/draft"
    output(call(skill, "edit_workspace", action="create", path=path, files=[]))
    failed = call(skill, "edit_workspace", action="append", path=path + "/a.txt", content="xx")
    assert failed.error_code == "file_too_large" and not failed.state_changed
    assert output(call(skill, action="read", path=path + "/a.txt"))["files"][0]["content"] == "seed"


def test_exclusive_source_create_does_not_replace_a_racing_file(skill, monkeypatch):
    path = create_package(skill)
    link = store.os.link

    def race(source, target):
        Path(target).write_text("concurrent", encoding="utf-8")
        return link(source, target)

    monkeypatch.setattr(store.os, "link", race)
    result = call(skill, "edit_workspace", action="create", path=path + "/race.txt", content="ours")
    assert result.error_code == "already_exists" and not result.state_changed
    assert output(call(skill, action="read", path=path + "/race.txt"))["files"][0]["content"] == "concurrent"
    assert not list((store.ROOT / "local_workspace" / "draft").glob("_write_*"))


@pytest.mark.parametrize("legacy,scope", [
    ("browse_mochi_files", "documents"), ("inspect_extension", "extensions"),
])
def test_legacy_read_denies_are_scoped_and_mixed_reads_preflight(skill, monkeypatch, legacy, scope):
    from mochi import tool_policy

    path = create_package(skill, **{"notes.txt": "source body"})
    doc = "documents/notes.md"
    output(call(skill, "edit_workspace", action="create", path=doc, content="document body"))
    monkeypatch.setattr(tool_policy, "_deny_set", {legacy})
    denied = doc if scope == "documents" else path + "/notes.txt"
    allowed = path + "/notes.txt" if scope == "documents" else doc
    result = call(skill, action="read", path=denied)
    assert result.error_code == "tool_denied" and not result.state_changed
    assert call(skill, action="list", path=scope).error_code == "tool_denied"
    assert call(skill, action="search", path="documents" if scope == "documents" else path, query="body").error_code == "tool_denied"
    output(call(skill, action="read", path=allowed))
    for action in ("guide", "report"):
        if scope == "extensions":
            assert call(skill, action=action, path=path).error_code == "tool_denied"
    output(call(skill, "edit_workspace", action="append", path=denied, content=" appended"))
    monkeypatch.setattr(documents, "read_file", lambda *a, **k: pytest.fail("Mixed read must preflight all scopes"))
    monkeypatch.setattr(store, "read_files", lambda *a, **k: pytest.fail("Mixed read must preflight all scopes"))
    assert call(skill, action="read", paths=[allowed, denied]).error_code == "tool_denied"
    assert {t["function"]["name"] for t in skill.available_tools()} == {
        "browse_workspace", "edit_workspace", "run_extension", "activate_extension",
    }
    assert tool_policy._call_log == {}


@pytest.mark.parametrize("legacy,scope", [
    ("save_mochi_file", "documents"), ("write_extension", "extensions"),
])
def test_legacy_write_denies_do_not_affect_other_scope_or_reading(skill, monkeypatch, legacy, scope):
    from mochi import tool_policy

    path = create_package(skill, **{"notes.txt": "source body"})
    doc = "documents/notes.md"
    output(call(skill, "edit_workspace", action="create", path=doc, content="document body"))
    monkeypatch.setattr(tool_policy, "_deny_set", {legacy})
    denied = doc if scope == "documents" else path + "/notes.txt"
    allowed = path + "/notes.txt" if scope == "documents" else doc
    assert call(skill, "edit_workspace", action="append", path=denied, content="blocked").error_code == "tool_denied"
    output(call(skill, "edit_workspace", action="append", path=allowed, content=" allowed"))
    output(call(skill, action="read", path=denied))
    if scope == "extensions":
        assert call(skill, "edit_workspace", action="create", path="extensions/local_denied/draft", files=[]).error_code == "tool_denied"
        assert not (store.ROOT / "local_denied").exists()
    assert "edit_workspace" in {t["function"]["name"] for t in skill.available_tools()}
    assert tool_policy._call_log == {}


def test_legacy_denied_root_avoids_source_inspection_and_marks_unavailable_areas(skill, monkeypatch):
    from mochi import tool_policy

    monkeypatch.setattr(tool_policy, "_deny_set", {"inspect_extension"})
    monkeypatch.setattr(registry, "get_skill_info_all", lambda: pytest.fail("Denied source must not be inspected"))
    root = output(call(skill, action="list"))
    areas = {area["path"]: area for area in root["areas"]}
    assert not areas["extensions"]["available"] and areas["extensions"]["reason"] == "tool_denied"
    assert areas["documents"]["available"] and areas["documents"]["write_available"]
    assert root["packages"] == [] and not root["packages_available"]
    monkeypatch.setattr(tool_policy, "_deny_set", {
        "browse_mochi_files", "save_mochi_file", "inspect_extension", "write_extension",
    })
    assert len(skill.get_tools()) == 4
    assert {t["function"]["name"] for t in skill.available_tools()} == {"run_extension", "activate_extension"}
    root = output(call(skill, action="list"))
    assert all(not area["available"] and not area["write_available"] for area in root["areas"])
    assert tool_policy._call_log == {}
