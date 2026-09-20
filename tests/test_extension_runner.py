"""Focused tests for disposable trusted-code execution; no Mochi runtime launch."""

import asyncio
import json
import os
from pathlib import Path
import pytest

from mochi.extensions import runner, store, template


@pytest.fixture(autouse=True)
def fresh_db():
    """Override the shared application fixture: these helpers do not need a DB."""


@pytest.fixture(autouse=True)
def mock_config():
    """No live config injection is needed by the import-light worker."""


@pytest.fixture
def extension(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    extension_id = "local_runner"
    draft = store.extension_root(extension_id) / "draft"
    draft.mkdir(parents=True)
    for name, content in template.files(extension_id).items():
        (draft / name).write_text(content, encoding="utf-8")
    return extension_id, draft


def run(extension, source=None, *, script="smoke.py", arguments=None, timeout=5):
    extension_id, draft = extension
    if source is not None:
        path = draft / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return asyncio.run(runner.run_extension(
        extension_id, script, arguments, timeout=timeout,
    ))


def _alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    # Zombies have terminated but may await reaping by the host's init process.
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists() and proc_stat.read_text().split(") ", 1)[1].startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _wait_for_file(path):
    for _ in range(400):
        if path.exists() and path.read_text().strip():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Child did not write {path.name}")


def test_generated_smoke_calls_actual_skill(extension):
    report = run(extension)
    assert report["success"], report
    assert report["execution_started"]
    assert report["exit_code"] == 0
    assert report["stdout"].strip() == "Hello from local_runner"
    assert report["cleanup_errors"] == []
    assert report["report_saved"]
    saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert saved["stdout"] == report["stdout"]
    assert runner.read_report(extension[0]) == saved
    assert runner.read_last_run(extension[0]) == saved
    assert not list(extension[1].parent.glob(".run-*"))


def test_generated_smoke_exposes_real_handler_failure(extension):
    handler = extension[1] / "handler.py"
    handler.write_text(handler.read_text().replace(
        "return SkillResult(output=text)",
        'return SkillResult(output="semantic failure detail", success=False)',
    ), encoding="utf-8")
    report = run(extension)
    assert not report["success"]
    assert report["exit_code"] != 0
    assert "semantic failure detail" in report["stdout"]
    assert "AssertionError" in report["stderr"]
    assert not report["timed_out"]


def test_exit_zero_does_not_claim_semantic_success(extension):
    report = run(extension, 'print("success=False")')
    assert report["success"] and report["exit_code"] == 0
    assert "Script completion only" in report["success_scope"]
    assert report["state_change_unknown"]
    assert "not a sandbox" in report["side_effects"]


def test_copy_relative_import_config_and_disposable_data(extension):
    extension_id, draft = extension
    (draft / "helper.py").write_text('TEXT = "relative helper"\n', encoding="utf-8")
    (draft / "handler.py").write_text(
        "from pathlib import Path\n"
        "from .helper import TEXT\n"
        "from mochi.skills.base import Skill, SkillResult\n"
        "class PersonalSkill(Skill):\n"
        "    async def execute(self, context):\n"
        "        Path(self.__module_file__).write_text('changed copy')\n"
        "        (self.data_dir / 'private.txt').write_text('test data')\n"
        "        return SkillResult(output=TEXT + self.config['suffix'])\n",
        encoding="utf-8",
    )
    original_handler = (draft / "handler.py").read_bytes()
    for area in ("current", "previous", "data"):
        directory = draft.parent / area
        directory.mkdir()
        (directory / "keep.txt").write_text("untouched", encoding="utf-8")
    report = run(extension,
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "from mochi.extensions.worker import run_candidate\n"
        "from mochi.skills.base import SkillContext\n"
        f"skill = run_candidate({extension_id!r}, Path(__file__).parent, Path(os.environ['MOCHI_EXTENSION_DATA_DIR']))\n"
        "skill.config = {'suffix': ' with config'}\n"
        "result = asyncio.run(skill.run(SkillContext(trigger='script')))\n"
        "print(result.output)\n"
        "assert result.success, result.output\n"
        "assert not {'mochi.config', 'mochi.db', 'mochi.diary', 'mochi.main'} & sys.modules.keys()\n",
    )
    assert report["success"], report
    assert report["stdout"].strip() == "relative helper with config"
    assert (draft / "handler.py").read_bytes() == original_handler
    for area in ("current", "previous", "data"):
        assert list((draft.parent / area).iterdir()) == [draft.parent / area / "keep.txt"]
        assert (draft.parent / area / "keep.txt").read_text() == "untouched"


def test_environment_and_arguments_are_explicit(extension, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-secret")
    monkeypatch.setenv("CUSTOM_PROVIDER_SECRET", "unknown-secret")
    monkeypatch.setenv("PYTHONPATH", "untrusted-inherited-path")
    arguments = ["two words", "$literal;value", "日本語"]
    report = run(extension,
        "import json, os, sys, tempfile\n"
        "assert not any(name in os.environ for name in "
        "('OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CUSTOM_PROVIDER_SECRET'))\n"
        "assert 'untrusted-inherited-path' not in os.environ['PYTHONPATH']\n"
        "assert tempfile.gettempdir() == os.environ['TMPDIR']\n"
        "print(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
        arguments=arguments,
    )
    assert report["success"], report
    assert json.loads(report["stdout"]) == arguments


def test_smoke_config_uses_typed_defaults_and_explicit_fixtures(extension, monkeypatch):
    handler = extension[1] / "handler.py"
    handler.write_text(handler.read_text(encoding="utf-8").replace(
        "    async def execute",
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.constructor_count = self.get_config('COUNT')\n\n"
        "    async def execute",
    ), encoding="utf-8")
    metadata = extension[1] / "SKILL.md"
    metadata.write_text(metadata.read_text(encoding="utf-8").replace(
        "type: tool\n",
        "type: tool\n"
        "config:\n"
        "  COUNT:\n"
        "    type: int\n"
        "    default: 3\n"
        "  ENABLED:\n"
        "    type: bool\n"
        "    default: false\n"
        "  RATIO:\n"
        "    type: float\n"
        "    default: 1.25\n"
        "  LABEL:\n"
        "    type: str\n"
        '    default: "example"\n',
    ), encoding="utf-8")
    monkeypatch.setenv("COUNT", "999")
    monkeypatch.setenv("SKILL_LOCAL_RUNNER_COUNT", "888")
    report = run(extension,
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "from mochi.extensions.worker import run_candidate\n"
        "skill = run_candidate('local_runner', Path(__file__).parent,\n"
        "    Path(os.environ['MOCHI_EXTENSION_DATA_DIR']), config={'COUNT': 7})\n"
        "assert skill.get_config('COUNT') == '7'\n"
        "assert skill.constructor_count == '7'\n"
        "assert skill.get_config('MISSING') == ''\n"
        "assert not {'mochi.config', 'mochi.db', 'mochi.diary', 'mochi.main'} & sys.modules.keys()\n"
        "print(json.dumps(skill.config))\n",
    )
    assert report["success"], report
    assert json.loads(report["stdout"]) == {
        "COUNT": 7, "ENABLED": False, "RATIO": 1.25, "LABEL": "example",
    }


def test_nested_python_script(extension):
    report = run(extension, 'print("nested")', script="checks/check.py")
    assert report["success"], report
    assert report["stdout"].strip() == "nested"


def test_installed_dependencies_are_available_in_worker(extension):
    import httpx

    report = run(extension, "import httpx\nprint(httpx.__version__)\n")
    assert report["success"], report
    assert report["stdout"].strip() == httpx.__version__


def test_legacy_config_table_defaults_are_available_in_worker(extension):
    metadata = extension[1] / "SKILL.md"
    metadata.write_text(metadata.read_text(encoding="utf-8") + (
        "\n## Config\n\n"
        "| Key | Type | Secret | Default | Description |\n"
        "|-----|------|--------|---------|-------------|\n"
        "| COUNT | int | no | 3 | Number of items |\n"
    ), encoding="utf-8")
    report = run(extension,
        "import os\n"
        "from pathlib import Path\n"
        "from mochi.extensions.worker import run_candidate\n"
        "skill = run_candidate('local_runner', Path(__file__).parent,\n"
        "    Path(os.environ['MOCHI_EXTENSION_DATA_DIR']))\n"
        "assert skill.config['COUNT'] == 3, skill.config\n",
    )
    assert report["success"], report


@pytest.mark.parametrize("script", [
    "../outside.py", "..\\outside.py", "/outside.py", "C:\\outside.py",
    "C:outside.py", "smoke.py:extra", "smoke.txt", "./smoke.py",
    "checks//test.py", "missing.py", "", "smoke.py\x00",
])
def test_rejects_invalid_script_paths(extension, script):
    with pytest.raises(store.ExtensionError):
        run(extension, script=script)
    assert not list(extension[1].parent.glob(".run-*"))


def test_rejects_linked_script(extension):
    target = extension[1].parent / "outside.py"
    target.write_text('print("outside")', encoding="utf-8")
    link = extension[1] / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Host does not permit creation of symlinks")
    with pytest.raises(store.ExtensionError):
        run(extension, script="linked.py")


@pytest.mark.parametrize("timeout", [0, -1, 61, 10**400, float("nan"), float("inf"), True, "30"])
def test_rejects_invalid_timeout(extension, timeout):
    with pytest.raises(store.ExtensionError) as error:
        run(extension, timeout=timeout)
    assert error.value.code == "invalid_timeout"


@pytest.mark.parametrize("arguments", ["value", [1], ["nul\x00"]])
def test_rejects_invalid_arguments(extension, arguments):
    with pytest.raises(store.ExtensionError) as error:
        run(extension, arguments=arguments)
    assert error.value.code == "invalid_arguments"


def test_bounded_output_is_drained_and_last_report_replaced(extension):
    report = run(extension,
        "import sys\n"
        "sys.stdout.write('o' * 200000)\n"
        "sys.stderr.write('e' * 200000)\n"
        "raise SystemExit(7)\n",
    )
    assert report["exit_code"] == 7
    assert not report["success"]
    assert report["output_truncated"]
    assert report["stdout"] == "o" * runner.OUTPUT_LIMIT
    assert report["stderr"] == "e" * runner.OUTPUT_LIMIT
    replacement = run(extension, 'print("replacement")')
    assert replacement["report_path"] == report["report_path"]
    saved = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
    assert saved["stdout"] == "replacement" + os.linesep
    assert not saved["output_truncated"]


def test_report_cap_handles_worst_case_json_escaping(extension):
    report = run(extension,
        "import os\n"
        "os.write(1, bytes(100000))\n"
        "os.write(2, bytes(100000))\n",
    )
    assert report["success"] and report["output_truncated"], report
    assert report["report_saved"]
    assert Path(report["report_path"]).stat().st_size <= store.MAX_FILE_BYTES
    assert runner.read_report(extension[0])["stdout"] == "\x00" * runner.OUTPUT_LIMIT


def test_report_reader_missing_and_corrupt(extension):
    assert runner.read_report(extension[0]) is None
    with pytest.raises(store.ExtensionError) as missing:
        runner.read_last_run(extension[0])
    assert missing.value.code == "no_run_report"
    report_path = extension[1].parent / "last-run.json"
    for text in ("not json", "[]"):
        report_path.write_text(text, encoding="utf-8")
        with pytest.raises(store.ExtensionError) as error:
            runner.read_report(extension[0])
        assert error.value.code == "invalid_report"


def test_timeout_cleans_descendants_and_keeps_diagnostics(extension):
    report = run(extension,
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "print('before timeout', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n",
        timeout=0.7,
    )
    assert report["timed_out"] and not report["success"], report
    assert report["execution_started"]
    assert report["stderr"] == "before timeout" + os.linesep
    assert report["cleanup_errors"] == []
    assert not _alive(int(report["stdout"].strip()))
    assert not list(extension[1].parent.glob(".run-*"))


def test_normal_exit_cleans_detached_stdio_descendant(extension):
    report = run(extension,
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n",
    )
    assert report["success"], report
    assert not _alive(int(report["stdout"].strip()))


def test_cancellation_cleans_tree_and_persists_interruption(extension):
    marker = extension[1].parent / "child-pid.txt"
    source = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n"
    )
    (extension[1] / "smoke.py").write_text(source, encoding="utf-8")

    async def cancel():
        task = asyncio.create_task(runner.run_extension(extension[0]))
        await _wait_for_file(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert not _alive(int(marker.read_text()))
    report = json.loads((extension[1].parent / "last-run.json").read_text(encoding="utf-8"))
    assert report["interrupted"] and not report["success"]
    assert report["execution_started"]
    assert report["cleanup_errors"] == []
    assert not list(extension[1].parent.glob(".run-*"))


def test_process_start_failure_is_not_execution(extension, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError("No Python process")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
    report = run(extension)
    assert not report["success"] and not report["execution_started"]
    assert report["exit_code"] is None
    assert "No Python process" in report["error"]
    assert report["report_saved"]


def test_cancellation_during_creation_reaps_unreleased_worker(extension, monkeypatch):
    real_launch = asyncio.create_subprocess_exec
    processes = []
    marker = extension[1].parent / "must-not-exist"
    (extension[1] / "smoke.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8",
    )

    async def cancel():
        spawned = asyncio.Event()

        async def slow_launch(*args, **kwargs):
            process = await real_launch(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await asyncio.sleep(0.1)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_launch)
        task = asyncio.create_task(runner.run_extension(extension[0]))
        await spawned.wait()
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert not marker.exists()
    assert len(processes) == 1 and not _alive(processes[0].pid)
    report = json.loads((extension[1].parent / "last-run.json").read_text(encoding="utf-8"))
    assert report["interrupted"] and not report["execution_started"]
    assert not list(extension[1].parent.glob(".run-*"))


def test_report_write_failure_returns_execution_facts(extension, monkeypatch):
    def fail(*args):
        raise OSError("report destination unavailable")

    monkeypatch.setattr(runner, "_write_run_report", fail)
    report = run(extension, 'print("actual output")')
    assert report["execution_started"] and report["exit_code"] == 0
    assert report["stdout"] == "actual output" + os.linesep
    assert not report["report_saved"]
    assert "report destination unavailable" in report["report_error"]


def test_cancellation_is_preserved_when_pending_spawn_fails(extension, monkeypatch):
    async def cancel():
        spawning = asyncio.Event()

        async def unavailable(*args, **kwargs):
            spawning.set()
            await asyncio.sleep(0.05)
            raise OSError("spawn failed after cancellation")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", unavailable)
        task = asyncio.create_task(runner.run_extension(extension[0]))
        await spawning.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    report = json.loads((extension[1].parent / "last-run.json").read_text(encoding="utf-8"))
    assert report["interrupted"] and not report["execution_started"]
    assert "spawn failed after cancellation" in report["error"]


def test_cleanup_failure_is_explicit_even_after_exit_zero(extension, monkeypatch):
    real_cleanup = runner._cleanup

    async def failed_cleanup(*args):
        errors = await real_cleanup(*args)
        return errors + ["simulated descendant cleanup failure"]

    monkeypatch.setattr(runner, "_cleanup", failed_cleanup)
    report = run(extension, 'print("finished")')
    assert report["exit_code"] == 0 and not report["success"]
    assert report["cleanup_errors"] == ["simulated descendant cleanup failure"]
    assert report["state_change_unknown"]


def test_script_syntax_error_is_returned_verbatim(extension):
    report = run(extension, "this is invalid Python:\n")
    assert not report["success"]
    assert report["exit_code"] != 0
    assert "SyntaxError" in report["stderr"]


@pytest.mark.skipif(os.name != "nt", reason="Windows job startup gate")
def test_windows_job_assignment_failure_never_executes_script(extension, monkeypatch):
    def fail(self, pid):
        raise OSError("Job assignment unavailable")

    monkeypatch.setattr(runner._WindowsJob, "assign", fail)
    report = run(extension, 'print("must not run")')
    assert not report["execution_started"] and not report["success"]
    assert report["stdout"] == ""
    assert "Job assignment unavailable" in report["error"]
