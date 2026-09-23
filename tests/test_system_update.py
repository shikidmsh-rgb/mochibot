"""Official updater contracts; never run Git, pip, or source updates here."""

from types import SimpleNamespace

import httpx
import pytest

from mochi.skills.base import SkillContext
from mochi.skills.system_update import handler
import mochi.update_service as updater

BEFORE = "a" * 40
TARGET = "b" * 40


@pytest.fixture
def release():
    return updater.ReleaseInfo(
        tag="v1.2.0", version="1.2.0", name="Release", notes="",
        url="https://github.com/shikidmsh-rgb/mochibot/releases/tag/v1.2.0",
        current_version="1.1.0", available=True,
    )


@pytest.fixture
def local_installation(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setattr(updater, "PROJECT_ROOT", root)
    monkeypatch.setattr(updater, "UPDATE_REQUEST_PATH", root / "data" / ".update_request")
    monkeypatch.setattr(updater, "UPDATE_RESULT_PATH", root / "data" / ".update_result")
    monkeypatch.setattr(updater, "_active_request_id", None)
    monkeypatch.setattr(updater, "_is_container", lambda: False)
    monkeypatch.setenv("MOCHIBOT_UPDATE_LAUNCHER", "1")
    calls = []
    state = {
        "head": BEFORE, "dirty": "", "ancestry": 0,
        "version": "1.2.0", "protected": "",
        "requirements": "", "fetch": 0, "merge": 0,
    }

    def git(*args, **kwargs):
        calls.append(args)
        if args == ("rev-parse", "--is-inside-work-tree"):
            return 0, "true"
        if args == ("rev-parse", "HEAD"):
            return 0, state["head"]
        if args[0] == "status":
            return 0, state["dirty"]
        if args == ("branch", "--show-current"):
            return 0, "main"
        if args[0] == "fetch":
            return state["fetch"], ""
        if args == ("rev-parse", "--verify", "FETCH_HEAD^{commit}"):
            return 0, TARGET
        if args[0] == "show":
            return 0, f'__version__ = "{state["version"]}"'
        if args[0] == "ls-tree":
            return 0, state["protected"]
        if args[0] == "merge-base":
            return state["ancestry"], ""
        if args[0] == "diff":
            return 0, state["requirements"]
        if args[0] == "merge":
            if not state["merge"]:
                state["head"] = TARGET
            return state["merge"], ""
        pytest.fail(f"Unexpected Git invocation: {args}")

    monkeypatch.setattr(updater, "_run_git", git)
    return root, state, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,current,expected", [
    ({"tag_name": "v1.2.0", "draft": False, "prerelease": False}, "1.1.0", True),
    ({"tag_name": "v1.2.0", "draft": False, "prerelease": False}, "1.3.0", False),
    ({"tag_name": "v1.2.0", "draft": True, "prerelease": False}, "1.1.0", "invalid_release"),
    ({"tag_name": "v1.2.0", "draft": False, "prerelease": True}, "1.1.0", "invalid_release"),
    ({"tag_name": "main", "draft": False, "prerelease": False}, "1.1.0", "invalid_release"),
])
async def test_only_official_stable_release_is_selected(monkeypatch, payload, current, expected):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(updater.httpx, "AsyncClient", Client)
    monkeypatch.setattr(updater, "read_version", lambda: current)
    if isinstance(expected, str):
        with pytest.raises(updater.UpdateError) as exc:
            await updater.check_for_update()
        assert exc.value.code == expected
    else:
        release = await updater.check_for_update()
        assert release.available is expected
    assert calls == [updater.LATEST_RELEASE_URL]


def test_successful_git_stderr_warning_does_not_corrupt_stdout(monkeypatch):
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="true\n", stderr="warning: safe directory\n",
    ))
    assert updater._run_git("rev-parse", "--is-inside-work-tree") == (0, "true")


@pytest.mark.asyncio
async def test_install_stages_without_exit_until_callback(local_installation, release, monkeypatch):
    import mochi.shutdown as shutdown

    async def latest():
        return release

    exits = []
    monkeypatch.setattr(handler, "check_for_update", latest)
    monkeypatch.setattr(shutdown, "request_process_exit", exits.append)
    context = SkillContext(
        trigger="tool_call", actor="main", source="chat", turn_id="turn1",
        owner_authorized=True, user_id=1, channel_id=2, transport="telegram",
        tool_name="install_system_update",
    )
    skill = handler.SystemUpdateSkill()
    result = await skill.execute(context)
    assert result.success and result.state_changed and result.after_delivery
    assert "现在尚未安装" in result.output
    assert exits == []
    assert not any(command[0] == "merge" for command in local_installation[2])
    repeated = await skill.execute(context)
    assert repeated.success and not repeated.state_changed and repeated.after_delivery is None
    context.turn_id = "owner-retry-after-undelivered-reply"
    retry = await skill.execute(context)
    assert retry.success and retry.after_delivery
    assert exits == []
    retry.after_delivery()
    assert exits == [shutdown.UPDATE_EXIT_CODE]
    exits.clear()
    result.after_delivery()
    assert exits == [shutdown.UPDATE_EXIT_CODE]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,owner", [
    ("runtime:free_time", True), ("runtime:self_reminder", True), ("chat", False),
])
async def test_update_cannot_be_authorized_by_autonomous_or_nonowner_context(source, owner, monkeypatch):
    async def unexpected_lookup():
        pytest.fail("unauthorized calls must not query release API")

    monkeypatch.setattr(handler, "check_for_update", unexpected_lookup)
    result = await handler.SystemUpdateSkill().execute(SkillContext(
        trigger="tool_call", actor="main", source=source, owner_authorized=owner,
        tool_name="install_system_update",
    ))
    assert not result.success and not result.state_changed


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,code", [
    ("dirty", " M local.py", "dirty_worktree"),
    ("ancestry", 1, "non_fast_forward"),
    ("protected", "data/mochi.db", "protected_data_paths"),
    ("version", "1.9.0", "release_version_mismatch"),
    ("fetch", 1, "official_fetch_failed"),
])
async def test_prepare_refuses_unsafe_installation(local_installation, release, field, value, code):
    _, state, calls = local_installation
    state[field] = value
    with pytest.raises(updater.UpdateError) as exc:
        await updater.prepare_update(release)
    assert exc.value.code == code
    assert not any(command[0] in {"merge", "reset", "stash", "clean"} for command in calls)


@pytest.mark.asyncio
async def test_direct_main_installation_is_refused(local_installation, release, monkeypatch):
    monkeypatch.delenv("MOCHIBOT_UPDATE_LAUNCHER")
    with pytest.raises(updater.UpdateError) as exc:
        await updater.prepare_update(release)
    assert exc.value.code == "update_launcher_required"
    assert local_installation[2] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dependencies_ok", [True, False])
async def test_apply_fast_forwards_exact_target_and_preserves_result_until_ack(
    local_installation, release, monkeypatch, dependencies_ok,
):
    _, state, calls = local_installation
    state["requirements"] = "requirements.txt"
    synchronized = []

    def sync(python):
        synchronized.append(python)
        return dependencies_ok

    monkeypatch.setattr(updater, "_sync_requirements", sync)
    prepared = await updater.prepare_update(release)
    staged = updater.stage_update(
        prepared, user_id=1, channel_id=2, transport="telegram", turn_id="turn1",
    )
    result = updater.apply_pending_update("isolated-python")
    assert result["ok"] is dependencies_ok
    assert result["code_updated"] is True
    assert result["version"] == "1.2.0"
    assert synchronized == ["isolated-python"]
    assert ("fetch", "--no-tags", updater.OFFICIAL_REMOTE_URL, "refs/tags/v1.2.0") in calls
    assert ("merge", "--ff-only", "--no-overwrite-ignore", TARGET) in calls
    assert not any(command[0] in {"reset", "stash", "clean", "pull"} for command in calls)
    assert not updater.UPDATE_REQUEST_PATH.exists()
    assert updater.peek_update_result() == result
    assert updater.ack_update_result("wrong-request") is False
    assert updater.peek_update_result() == result
    assert updater.ack_update_result(staged["request_id"]) is True
    assert updater.peek_update_result() is None
    if not dependencies_ok:
        assert "依赖安装失败" in result["message"]


@pytest.mark.asyncio
async def test_apply_rechecks_head_and_pending_request_is_not_overwritten(local_installation, release):
    _, state, calls = local_installation
    prepared = await updater.prepare_update(release)
    updater.stage_update(
        prepared, user_id=1, channel_id=2, transport="telegram", turn_id="turn1",
    )
    with pytest.raises(updater.UpdateError) as exc:
        updater.stage_update(
            prepared, user_id=1, channel_id=3, transport="telegram", turn_id="turn2",
        )
    assert exc.value.code == "update_pending"
    state["head"] = "c" * 40
    result = updater.apply_pending_update()
    assert not result["ok"] and result["code_updated"] is False
    assert "本地代码发生变化" in result["message"]
    assert not any(command[0] == "merge" for command in calls)


def test_module_cli_delegates_to_the_same_service(monkeypatch, capsys):
    monkeypatch.setattr(updater, "apply_pending_update", lambda: {
        "ok": False, "message": "更新未执行：更新请求无效，未修改代码。",
    })
    assert updater.main() == 1
    assert "更新未执行" in capsys.readouterr().out
