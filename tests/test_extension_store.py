"""Personal code publication preserves current code on failure and keeps data."""

import os
from pathlib import Path
import types

import pytest

from mochi.extensions import store


@pytest.fixture(autouse=True)
def fresh_db():
    """These filesystem tests do not use the shared Mochi database."""


@pytest.fixture(autouse=True)
def mock_config():
    """Do not load runtime configuration in extension unit tests."""


@pytest.fixture(autouse=True)
def extension_root(tmp_path, monkeypatch):
    root = tmp_path / "extensions"
    monkeypatch.setattr(store, "ROOT", root)
    monkeypatch.setattr(store, "_ERRORS", {})
    return root


def metadata(name="local_example", description="Echo a value"):
    return f"""---
name: {name}
type: tool
description: Test personal tool.
---

## Tools
### {name}_echo (on_demand)
{description}

| Parameter | Type | Required | Description |
|---|---|---|---|
| value | string | yes | Value to echo. |
"""


HANDLER = """from mochi.skills.base import Skill, SkillResult

class Example(Skill):
    async def execute(self, context):
        return SkillResult(output=context.args.get("value", ""))
"""


def make_draft(name="local_example"):
    root = store.extension_root(name) / "draft"
    root.mkdir(parents=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "handler.py").write_bytes(HANDLER.encode("utf-8"))
    (root / "SKILL.md").write_text(metadata(name), encoding="utf-8")
    return root


def test_ids_root_resolution_and_metadata_only_listing(extension_root, monkeypatch):
    for name in ["../local_no", "_local_no", "local_A", "local_", "local_" + "a" * 35]:
        with pytest.raises(store.ExtensionError, match="Extension ID"):
            store.extension_root(name)
    assert not extension_root.exists()
    assert store.extension_root("local_" + "a" * 34).parent == extension_root
    draft = make_draft()
    (draft / "handler.py").write_text("raise RuntimeError('must not run')", encoding="utf-8")
    ignored = extension_root / "_local_disabled" / "current"
    ignored.mkdir(parents=True)
    assert store.list_extensions() == [{
        "name": "local_example", "has_draft": True,
        "installed": False, "has_previous": False,
    }]
    monkeypatch.setattr(store, "ROOT", extension_root.parent / "another")
    assert store.list_extensions() == []


def test_scaffold_accepts_complete_authored_files_and_never_overwrites(monkeypatch):
    from mochi.extensions import template

    monkeypatch.setattr(template, "files", lambda name: {
        "__init__.py": "", "SKILL.md": metadata(name),
        "handler.py": HANDLER, "smoke.py": "print('template')",
    })
    result = store.scaffold(
        "local_example", skill_md=metadata(), handler_py=HANDLER,
        smoke_py="print('authored')",
    )
    assert result["entrypoint"] == "smoke.py"
    assert result["generated"] == {"__init__.py": ""}
    assert set(result["created_paths"]) == {"__init__.py", "SKILL.md", "handler.py", "smoke.py"}
    assert store.read_files("local_example", ["smoke.py"])["files"][0]["content"] == "print('authored')"
    with pytest.raises(store.ExtensionError) as caught:
        store.scaffold("local_example", smoke_py="overwrite")
    assert caught.value.code == "draft_exists"
    assert not caught.value.state_changed
    assert "authored" in store.read_files("local_example", ["smoke.py"])["files"][0]["content"]
    default = store.scaffold("local_defaults")
    assert "smoke.py" in default["generated"]


def test_scaffold_current_copy_and_explicit_replacements():
    draft = make_draft()
    (draft / "helpers.py").write_text("VALUE = 1", encoding="utf-8")
    draft.rename(draft.parent / "current")
    result = store.scaffold("local_example", handler_py=HANDLER + "\n# new draft")
    assert result["copied_current"]
    assert "helpers.py" in result["created_paths"]
    assert "smoke.py" in result["created_paths"]
    assert store.read_files("local_example", ["handler.py"], area="current")["files"][0]["content"] == HANDLER
    assert "new draft" in store.read_files("local_example", ["handler.py"])["files"][0]["content"]


@pytest.mark.parametrize("path", [
    "../outside.py", "nested/../outside.py", r"nested\..\outside.py", "/root.py",
    r"C:\outside.py", r"\\server\share\a.py", "./a.py", "a//b.py", "a.py:stream",
    "CON.py", "a. /b.py", "bad\x00.py", "folder/",
])
def test_file_operations_reject_traversal_and_windows_aliases(path):
    make_draft()
    operations = [
        lambda: store.write_file("local_example", path, "no"),
        lambda: store.read_files("local_example", [path]),
        lambda: store.edit_file("local_example", path, "old", "new"),
        lambda: store.remove_file("local_example", path),
    ]
    for operation in operations:
        with pytest.raises(store.ExtensionError) as caught:
            operation()
        assert caught.value.code in {"invalid_path", "unsafe_path"}


def test_edit_atomic_write_and_multifile_read_bounds(monkeypatch):
    draft = make_draft()
    assert store.write_file("local_example", r"helpers\text.txt", "你好abcdef")["bytes"] == 12
    result = store.read_files("local_example", ["helpers/text.txt", "SKILL.md"], offset=1, limit=5)
    assert result["files"][0]["content"] == "好abcd"
    assert result["files"][0]["next_offset"] == 6
    assert result["files"][1]["content"] == ""
    assert result["truncated"]
    assert result["draft_tree"] == result["tree"]
    store.edit_file("local_example", "helpers/text.txt", "abcdef", "aaa")
    for old, code in [("absent", "stale_edit"), ("aa", "nonunique_edit")]:
        with pytest.raises(store.ExtensionError) as caught:
            store.edit_file("local_example", "helpers/text.txt", old, "x")
        assert caught.value.code == code
    original = os.replace

    def fail_write(source, target):
        if Path(target).name == "text.txt":
            raise OSError("replacement failed")
        return original(source, target)

    monkeypatch.setattr(store.os, "replace", fail_write)
    with pytest.raises(store.ExtensionError):
        store.write_file("local_example", "helpers/text.txt", "changed")
    assert (draft / "helpers" / "text.txt").read_text(encoding="utf-8") == "你好aaa"
    assert not list(draft.rglob("_write_*"))
    with pytest.raises(store.ExtensionError):
        store.remove_file("local_example", "helpers")
    assert store.remove_file("local_example", "helpers/text.txt")["state_changed"]
    for area in ["data", "../data", "pending"]:
        with pytest.raises(store.ExtensionError):
            store.read_files("local_example", [], area=area)


def test_size_limits_before_write_or_copy(monkeypatch, tmp_path):
    draft = make_draft()
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 4)
    with pytest.raises(store.ExtensionError) as caught:
        store.write_file("local_example", "large.txt", "你好")
    assert caught.value.code == "file_too_large"
    assert not (draft / "large.txt").exists()
    with pytest.raises(store.ExtensionError):
        store.copy_package(draft, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_entry_quota_cannot_leave_an_unreadable_draft(monkeypatch):
    draft = make_draft()
    monkeypatch.setattr(store, "MAX_PACKAGE_ENTRIES", 5)
    with pytest.raises(store.ExtensionError) as caught:
        store.write_file("local_example", "one/two/three.txt", "too many entries")
    assert caught.value.code == "package_too_large"
    assert not (draft / "one").exists()
    assert len(store.read_files("local_example", [])["tree"]) == 3


def test_publish_keeps_one_previous_and_extension_data_survives():
    draft = make_draft()
    (draft / "empty_helper_directory").mkdir()
    data = draft.parent / "data"
    data.mkdir()
    (data / "owned.json").write_text('{"kept":true}', encoding="utf-8")
    result = store.publish("local_example", draft)
    assert result["installed"] and result["state_changed"] and not result["has_previous"]
    assert "pending_restart" not in result
    assert result["tool_names"] == ["local_example_echo"]
    assert (draft.parent / "current" / "empty_helper_directory").is_dir()
    version_two = HANDLER + "\n# version two"
    store.write_file("local_example", "handler.py", version_two)
    assert store.read_files("local_example", ["handler.py"], area="current")["files"][0]["content"] == HANDLER
    assert store.publish("local_example", draft)["has_previous"]
    assert store.read_files("local_example", ["handler.py"], area="current")["files"][0]["content"] == version_two
    assert store.read_files("local_example", ["handler.py"], area="previous")["files"][0]["content"] == HANDLER
    version_three = HANDLER + "\n# version three"
    store.write_file("local_example", "handler.py", version_three)
    store.publish("local_example", draft)
    assert store.read_files("local_example", ["handler.py"], area="current")["files"][0]["content"] == version_three
    assert store.read_files("local_example", ["handler.py"], area="previous")["files"][0]["content"] == version_two
    assert {path.name for path in draft.parent.iterdir()} == {"draft", "current", "previous", "data"}
    assert (data / "owned.json").read_text(encoding="utf-8") == '{"kept":true}'


def test_publish_uses_separate_source_without_importing_or_renaming_it(tmp_path):
    draft = make_draft()
    source = tmp_path / "runtime_copy"
    store.copy_package(draft, source)
    (source / "__init__.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    (source / "handler.py").write_text("raise RuntimeError('must not import handler')", encoding="utf-8")
    store.write_file("local_example", "handler.py", "invalid draft(")
    result = store.publish("local_example", source)
    assert result["installed"] and result["has_draft"]
    assert (source / "handler.py").read_bytes() == (draft.parent / "current" / "handler.py").read_bytes()
    assert (source / "__init__.py").is_file()
    assert not (draft.parent / "data").exists()


def test_failed_validation_keeps_current_and_previous_then_success_clears_error():
    draft = make_draft()
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version two")
    store.publish("local_example", draft)
    current = (draft.parent / "current" / "handler.py").read_bytes()
    previous = (draft.parent / "previous" / "handler.py").read_bytes()
    store.write_file("local_example", "handler.py", "def syntax(")
    with pytest.raises(store.ExtensionError) as caught:
        store.publish("local_example", draft)
    assert caught.value.code == "invalid_python"
    assert not caught.value.state_changed
    assert (draft.parent / "current" / "handler.py").read_bytes() == current
    assert (draft.parent / "previous" / "handler.py").read_bytes() == previous
    assert "publish_error" in store.list_extensions()[0]
    store.write_file("local_example", "handler.py", HANDLER)
    result = store.publish("local_example", draft)
    assert "publish_error" not in result
    assert "publish_error" not in store.list_extensions()[0]


@pytest.mark.parametrize("failure_step", ["backup_previous", "move_current", "publish"])
def test_failed_publish_restores_current_and_previous(monkeypatch, failure_step):
    draft = make_draft()
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version two")
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version three")
    current = (draft.parent / "current" / "handler.py").read_bytes()
    previous = (draft.parent / "previous" / "handler.py").read_bytes()
    untouched = draft.parent / "_loaded_other_process"
    untouched.mkdir()
    original = os.replace

    def fail_publish(source, target):
        source, target = Path(source), Path(target)
        if (
            failure_step == "backup_previous" and source.name == "previous" and target.name.startswith("_previous_")
            or failure_step == "move_current" and source.name == "current"
            or failure_step == "publish" and source.name.startswith("_publish_")
        ):
            raise OSError("injected publication failure")
        return original(source, target)

    monkeypatch.setattr(store.os, "replace", fail_publish)
    with pytest.raises(store.ExtensionError) as caught:
        store.publish("local_example", draft)
    assert caught.value.code == "publish_failed"
    assert not caught.value.state_changed
    assert (draft.parent / "current" / "handler.py").read_bytes() == current
    assert (draft.parent / "previous" / "handler.py").read_bytes() == previous
    assert "injected publication failure" in store.list_extensions()[0]["publish_error"]
    assert not list(draft.parent.glob("_publish_*"))
    assert not list(draft.parent.glob("_previous_*"))
    assert untouched.is_dir()


@pytest.mark.parametrize("failure_step", ["copy", "stage_validation"])
def test_partial_or_invalid_copy_does_not_change_current(monkeypatch, failure_step):
    draft = make_draft()
    store.publish("local_example", draft)
    original = store.copy_package

    def fail_copy(source, destination):
        if failure_step == "copy":
            destination.mkdir()
            (destination / "handler.py").write_text(HANDLER, encoding="utf-8")
            raise OSError("copy interrupted")
        result = original(source, destination)
        (destination / "handler.py").write_text("invalid syntax(", encoding="utf-8")
        return result

    monkeypatch.setattr(store, "copy_package", fail_copy)
    with pytest.raises(store.ExtensionError) as caught:
        store.publish("local_example", draft)
    assert caught.value.code == ("publish_failed" if failure_step == "copy" else "invalid_python")
    assert not caught.value.state_changed
    assert (draft.parent / "current" / "handler.py").read_text(encoding="utf-8") == HANDLER
    assert not (draft.parent / "previous").exists()
    assert not list(draft.parent.glob("_publish_*"))


def test_failed_rollback_reports_changed_state_and_preserves_recovery_code(monkeypatch):
    draft = make_draft()
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version two")
    store.publish("local_example", draft)
    original = os.replace

    def fail_current(source, target):
        if Path(target).name == "current":
            raise OSError("current inaccessible")
        return original(source, target)

    monkeypatch.setattr(store.os, "replace", fail_current)
    with pytest.raises(store.ExtensionError) as caught:
        store.publish("local_example", draft)
    assert caught.value.code == "publish_rollback_failed"
    assert caught.value.state_changed
    assert "Restore current" in str(caught.value)
    assert "# version two" in (draft.parent / "previous" / "handler.py").read_text(encoding="utf-8")
    backup, = draft.parent.glob("_previous_*")
    assert (backup / "handler.py").read_text(encoding="utf-8") == HANDLER


def test_postcommit_cleanup_failure_is_reported_without_failing_publication(monkeypatch, caplog):
    draft = make_draft()
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version two")
    store.publish("local_example", draft)
    store.write_file("local_example", "handler.py", HANDLER + "\n# version three")
    original = store._remove_package

    def fail_cleanup(path):
        if path.name.startswith("_previous_"):
            raise OSError("backup busy")
        return original(path)

    monkeypatch.setattr(store, "_remove_package", fail_cleanup)
    result = store.publish("local_example", draft)
    assert result["state_changed"] and result["installed"]
    assert "backup busy" in result["publish_cleanup_error"]
    assert "publish_error" not in result
    assert "# version three" in (draft.parent / "current" / "handler.py").read_text(encoding="utf-8")
    assert "# version two" in (draft.parent / "previous" / "handler.py").read_text(encoding="utf-8")
    assert "backup busy" in caplog.text


def test_incomplete_candidate_is_error_not_false_success():
    draft = make_draft()
    (draft / "__init__.py").unlink()
    with pytest.raises(store.ExtensionError) as caught:
        store.publish("local_example", draft)
    assert caught.value.code == "incomplete_package"
    assert not caught.value.state_changed
    assert not (draft.parent / "current").exists()


def test_links_and_reparse_points_rejected_in_reads_copies_and_publish(tmp_path, monkeypatch):
    draft = make_draft()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("secret", encoding="utf-8")
    link = draft / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires a Windows privilege.")
    for operation in [
        lambda: store.read_files("local_example", ["linked/secret.py"]),
        lambda: store.write_file("local_example", "linked/secret.py", "overwrite"),
        lambda: store.remove_file("local_example", "linked/secret.py"),
        lambda: store.publish("local_example", draft),
        lambda: store.copy_package(draft, tmp_path / "copied"),
    ]:
        with pytest.raises(store.ExtensionError) as caught:
            operation()
        assert caught.value.code == "unsafe_path"
    assert (outside / "secret.py").read_text(encoding="utf-8") == "secret"


def test_windows_reparse_flag_is_rejected_without_symlink_privilege(monkeypatch):
    draft = make_draft()
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == draft:
            return types.SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(store.ExtensionError) as caught:
        store.read_files("local_example", ["handler.py"])
    assert caught.value.code == "unsafe_path"
