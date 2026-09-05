"""Aggregate filesystem contract for Main's private Mochi Files space."""

from __future__ import annotations

import os

import pytest

import mochi.mochi_files_store as store


def test_mochi_files_storage_contract(tmp_path, monkeypatch):
    def use_root(name: str):
        root = tmp_path / name
        monkeypatch.setattr(store, "DATA_DIR", root)
        return root

    use_root("paths")
    invalid_paths = (
        "/absolute.md",
        "../escape.md",
        "folder/../escape.md",
        ".hidden.md",
        "folder/.hidden/file.md",
        r"folder\file.md",
        "C:/drive.md",
        "folder//ambiguous.md",
        "./current.md",
        "CON.md",
        "bad?.md",
        "not-markdown.txt",
    )
    for path in invalid_paths:
        with pytest.raises(store.InvalidPathError):
            store.create_file(path, "nope")

    assert store.create_file("works/story.md", "开头")["bytes"] == 6
    with pytest.raises(store.FileConflictError):
        store.create_file("works/story.md", "overwrite")
    assert (
        store.DATA_DIR / store.ACTIVE_DIRNAME / "works" / "story.md"
    ).read_bytes() == "开头".encode()

    store.append_file("works/story.md", "\n第二段")
    active_story = (
        store.DATA_DIR / store.ACTIVE_DIRNAME / "works" / "story.md"
    )
    previous_story = (
        store.DATA_DIR / store.PREVIOUS_DIRNAME / "works" / "story.md"
    )
    assert active_story.read_text(encoding="utf-8") == "开头\n第二段"
    assert previous_story.read_text(encoding="utf-8") == "开头"

    store.edit_file("works/story.md", "第二段", "结尾")
    assert active_story.read_text(encoding="utf-8") == "开头\n结尾"
    assert previous_story.read_text(encoding="utf-8") == "开头\n第二段"
    store.append_file("works/story.md", "\n结尾")
    unchanged = active_story.read_bytes()
    for old_text in ("不存在", "结尾"):
        with pytest.raises(store.FileConflictError):
            store.edit_file("works/story.md", old_text, "x")
        assert active_story.read_bytes() == unchanged
    store.create_file("works/overlap.md", "aaa")
    with pytest.raises(store.FileConflictError):
        store.edit_file("works/overlap.md", "aa", "x")
    assert (
        store.DATA_DIR / store.ACTIVE_DIRNAME / "works" / "overlap.md"
    ).read_text(encoding="utf-8") == "aaa"

    active_root = store.DATA_DIR / store.ACTIVE_DIRNAME
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath("secret.md").write_text("secret", encoding="utf-8")
    link = active_root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pass
    else:
        with pytest.raises(store.InvalidPathError):
            store.read_file("linked/secret.md")
        assert not any(item["path"].startswith("linked/") for item in store.list_files()["files"])

    (active_root / "directory.md").mkdir()
    with pytest.raises(store.InvalidPathError):
        store.read_file("directory.md")

    use_root("file-limit")
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 4)
    with pytest.raises(store.QuotaExceededError):
        store.create_file("too-big.md", "你好")

    use_root("count-limit")
    monkeypatch.setattr(store, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(store, "MAX_ACTIVE_FILES", 2)
    store.create_file("one.md", "1")
    store.create_file("two.md", "2")
    with pytest.raises(store.QuotaExceededError):
        store.create_file("three.md", "3")

    use_root("combined-limit")
    monkeypatch.setattr(store, "MAX_ACTIVE_FILES", 100)
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 11)
    store.create_file("quota.md", "12345")
    store.append_file("quota.md", "")
    store.append_file("quota.md", "6")
    with pytest.raises(store.QuotaExceededError):
        store.append_file("quota.md", "7")
    assert (
        store.DATA_DIR / store.ACTIVE_DIRNAME / "quota.md"
    ).read_text(encoding="utf-8") == "123456"
    assert (
        store.DATA_DIR / store.PREVIOUS_DIRNAME / "quota.md"
    ).read_text(encoding="utf-8") == "12345"

    use_root("orphan-previous")
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 10)
    orphan = store.DATA_DIR / store.PREVIOUS_DIRNAME / "orphan.md"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("123456", encoding="utf-8")
    with pytest.raises(store.QuotaExceededError):
        store.create_file("orphan.md", "78901")
    assert orphan.read_text(encoding="utf-8") == "123456"

    use_root("backup-failure")
    monkeypatch.setattr(store, "MAX_TOTAL_BYTES", 1000)
    store.create_file("safe.md", "before")
    original_replace = os.replace

    def fail_previous(source, destination):
        if store.PREVIOUS_DIRNAME in str(destination):
            raise OSError("simulated backup failure")
        return original_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", fail_previous)
    with pytest.raises(store.StorageIOError):
        store.append_file("safe.md", " after")
    safe_file = store.DATA_DIR / store.ACTIVE_DIRNAME / "safe.md"
    assert safe_file.read_text(encoding="utf-8") == "before"
    assert not list(store.DATA_DIR.rglob("*.tmp"))
    monkeypatch.setattr(store.os, "replace", original_replace)

    use_root("browse")
    monkeypatch.setattr(store, "MAX_SEARCH_EXCERPT_CHARS", 12)
    store.create_file("z.md", "needle-" + ("z" * 30))
    store.create_file("a.md", "root file")
    store.create_file("a/first.md", ("a" * 20) + "needle" + ("b" * 20) + "needle")
    store.create_file("a/second.md", "你好世界")
    store.append_file("z.md", "-active")

    listing = store.list_files(limit=2)
    assert [item["path"] for item in listing["files"]] == [
        "a.md",
        "a/first.md",
    ]
    assert listing["complete"] is False
    assert listing["next_offset"] == 2
    assert store.list_files(path="a")["total"] == 2
    assert all(
        not item["path"].startswith(store.PREVIOUS_DIRNAME)
        for item in store.list_files()["files"]
    )

    search = store.search_files("needle", limit=1)
    assert search["total_matches"] == 3
    assert search["count"] == 1
    assert search["complete"] is False
    assert len(search["matches"][0]["excerpt"]) <= store.MAX_SEARCH_EXCERPT_CHARS
    assert store.search_files("needle", offset=1, limit=2)["complete"] is True
    assert store.search_files(
        "needle", path="a/first.md"
    )["total_matches"] == 2
    assert store.search_files("needle", path="a")["total_matches"] == 2
    assert store.search_files("needle-", path="z.md")["total_matches"] == 1

    page = store.read_file("a/second.md", offset=1, limit=2)
    assert page["content"] == "好世"
    assert page["next_offset"] == 3
    final_page = store.read_file(
        "a/second.md", offset=page["next_offset"], limit=2
    )
    assert final_page["content"] == "界"
    assert final_page["complete"] is True
    assert not list(store.DATA_DIR.rglob("*.tmp"))
