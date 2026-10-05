import subprocess

import pytest

from mochi import update_service as updater


@pytest.mark.asyncio
async def test_update_uses_real_git_without_overwriting_local_changes_or_user_data(tmp_path, monkeypatch):
    def git(path, *args):
        return subprocess.check_output(
            ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.test",
             "-c", "commit.gpgsign=false", *args],
            text=True, stderr=subprocess.STDOUT,
        ).strip()

    upstream, installed = tmp_path / "upstream", tmp_path / "installed"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    (upstream / ".gitignore").write_text(".env\ndata/\n", encoding="utf-8")
    (upstream / "mochi").mkdir()
    version = upstream / "mochi" / "__init__.py"
    version.write_text('__version__ = "1.1.0"\n', encoding="utf-8")
    (upstream / "requirements.txt").write_text("", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Initial fixture")
    git(tmp_path, "clone", str(upstream), str(installed))
    before = git(installed, "rev-parse", "HEAD")
    version.write_text('__version__ = "1.2.0"\n', encoding="utf-8")
    (upstream / "requirements.txt").write_text("# Changed dependencies\n", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "Release fixture")
    git(upstream, "tag", "v1.2.0")
    target = git(upstream, "rev-parse", "HEAD")
    (installed / "data").mkdir()
    protected = {installed / ".env": b"private configuration", installed / "data" / "core.md": b"Personal Core"}
    for path, content in protected.items():
        path.write_bytes(content)
    monkeypatch.setattr(updater, "PROJECT_ROOT", installed)
    monkeypatch.setattr(updater, "OFFICIAL_REMOTE_URL", str(upstream))
    monkeypatch.setattr(updater, "UPDATE_REQUEST_PATH", installed / "data" / ".update_request")
    monkeypatch.setattr(updater, "UPDATE_RESULT_PATH", installed / "data" / ".update_result")
    monkeypatch.setattr(updater, "_active_request_id", None)
    monkeypatch.setattr(updater, "_is_container", lambda: False)
    monkeypatch.setattr(updater, "_sync_requirements", lambda _python: False)
    monkeypatch.setenv("MOCHIBOT_UPDATE_LAUNCHER", "1")
    release = updater.ReleaseInfo("v1.2.0", "1.2.0", "Fixture", "", "", "1.1.0", True)
    prepared = await updater.prepare_update(release)
    installed_version = installed / "mochi" / "__init__.py"
    original = installed_version.read_bytes()
    installed_version.write_bytes(b"uncommitted owner changes")
    updater.stage_update(prepared, user_id=1, channel_id=1, transport="fake", turn_id="first")
    blocked = updater.apply_pending_update()
    assert not blocked["ok"] and not blocked["code_updated"]
    assert installed_version.read_bytes() == b"uncommitted owner changes"
    assert git(installed, "rev-parse", "HEAD") == before
    installed_version.write_bytes(original)
    prepared = await updater.prepare_update(release)
    updater.stage_update(prepared, user_id=1, channel_id=1, transport="fake", turn_id="second")
    result = updater.apply_pending_update()
    assert git(installed, "rev-parse", "HEAD") == target
    assert result["code_updated"] and not result["ok"]
    assert updater.peek_update_result() == result
    assert all(path.read_bytes() == content for path, content in protected.items())
