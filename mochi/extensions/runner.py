"""Bounded execution of trusted local Python, not filesystem/network containment.

Only default credentials are withheld. Scripts can still explicitly read host
files/configuration or cause external side effects, which cleanup cannot undo.
Cleanup targets the Windows job or POSIX process group; deliberately escaping a
POSIX group is not contained by this trusted-code workflow.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import signal
import sys
from tempfile import TemporaryDirectory
import time

from mochi.extensions import store


OUTPUT_LIMIT = 16_384  # Bytes per stream; even JSON-escaped reports fit the store limit.
MAX_TIMEOUT = 60
_CLEANUP_TIMEOUT = 5


class _WindowsJob:
    """Kill the PID's descendants even if the initial process has already exited."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._accounting = Accounting
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "QueryInformationJobObject": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
                wintypes.BOOL,
            ),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes = args
            function.restype = result
        self.handle = self._kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self._kernel.CloseHandle(self.handle)
            self.handle = None
            raise error

    def assign(self, pid: int) -> None:
        process = self._kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        if not process:
            raise self._ctypes.WinError(self._ctypes.get_last_error())
        try:
            if not self._kernel.AssignProcessToJobObject(self.handle, process):
                raise self._ctypes.WinError(self._ctypes.get_last_error())
        finally:
            self._kernel.CloseHandle(process)

    async def close(self) -> list[str]:
        errors = []
        if self.handle:
            if not self._kernel.TerminateJobObject(self.handle, 1):
                errors.append(f"TerminateJobObject: {self._ctypes.WinError(self._ctypes.get_last_error())}")
            deadline = time.monotonic() + _CLEANUP_TIMEOUT
            while True:
                accounting = self._accounting()
                if not self._kernel.QueryInformationJobObject(
                    self.handle, 1, self._ctypes.byref(accounting),
                    self._ctypes.sizeof(accounting), None,
                ):
                    errors.append(f"QueryInformationJobObject: {self._ctypes.WinError(self._ctypes.get_last_error())}")
                    break
                if not accounting.ActiveProcesses:
                    break
                if time.monotonic() >= deadline:
                    errors.append(f"Job cleanup incomplete: {accounting.ActiveProcesses} process(es) still active")
                    break
                await asyncio.sleep(0.01)
            if not self._kernel.CloseHandle(self.handle):
                errors.append(f"CloseHandle(job): {self._ctypes.WinError(self._ctypes.get_last_error())}")
            self.handle = None
        return errors


def _environment(data_dir: Path, scratch_dir: Path) -> dict[str, str]:
    env = {
        key: os.environ[key] for key in ("SYSTEMROOT", "SystemRoot", "WINDIR")
        if key in os.environ
    }
    if os.name == "nt":
        windows = env.get("SYSTEMROOT") or env.get("SystemRoot") or env.get("WINDIR", "")
        env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), str(Path(windows) / "System32")))
    else:
        env["PATH"] = os.defpath
    env.update({
        "PYTHONPATH": os.pathsep.join(dict.fromkeys([
            str(Path(__file__).resolve().parents[2]),
            *(path for path in sys.path if path and Path(path).is_dir()),
        ])),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "MOCHI_EXTENSION_DATA_DIR": str(data_dir),
        "TMP": str(scratch_dir),
        "TEMP": str(scratch_dir),
        "TMPDIR": str(scratch_dir),
    })
    return env


def _script_path(package: Path, script: str) -> Path:
    if not isinstance(script, str) or not script or len(script) > 240 or "\x00" in script:
        raise store.ExtensionError("invalid_script", "script must be a relative Python file")
    windows = PureWindowsPath(script)
    relative = PurePosixPath(script.replace("\\", "/"))
    if (
        windows.drive or windows.root or relative.is_absolute()
        or any(part in ("", ".", "..") for part in script.replace("\\", "/").split("/"))
        or ":" in script or relative.suffix != ".py"
    ):
        raise store.ExtensionError("invalid_script", "script must be a relative .py file without traversal")
    path = package.joinpath(*relative.parts)
    for candidate in (path, *path.parents):
        if candidate == package:
            break
        if candidate.is_symlink() or (
            hasattr(candidate, "is_junction") and candidate.is_junction()
        ):
            raise store.ExtensionError("invalid_script", "script paths cannot contain links")
    if not path.is_file() or not path.resolve().is_relative_to(package.resolve()):
        raise store.ExtensionError("invalid_script", "script must be an existing draft Python file")
    return path


async def _drain(stream, retained: bytearray, truncated: list[bool]) -> None:
    while chunk := await stream.read(8192):
        remaining = max(0, OUTPUT_LIMIT - len(retained))
        retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated[0] = True


def _write_run_report(extension_id: str, report: dict) -> Path:
    with store._operation():
        path = store.extension_root(extension_id) / "last-run.json"
        store._atomic_write(path, store._content_bytes(json.dumps(report, ensure_ascii=False)))
        return path


def read_report(extension_id: str) -> dict | None:
    """Read the single bounded report using the same link checks as draft files."""
    with store._operation():
        path = store._safe_path(store.extension_root(extension_id) / "last-run.json")
        if not path.exists():
            return None
        try:
            report = json.loads(store._read_bytes(path))
        except (ValueError, UnicodeError) as exc:
            raise store.ExtensionError("invalid_report", "Last-run report is not valid JSON") from exc
        if not isinstance(report, dict):
            raise store.ExtensionError("invalid_report", "Last-run report must contain a JSON object")
        return report


def read_last_run(extension_id: str) -> dict:
    """Return the last report, or an explicit missing-report fact for inspection."""
    report = read_report(extension_id)
    if report is None:
        raise store.ExtensionError("no_run_report", "This extension has no saved run report")
    return report


async def _cleanup(process, job, readers: list[asyncio.Task]) -> list[str]:
    errors = []
    if job is not None:
        errors.extend(await job.close())
    elif process is not None and os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            errors.append(f"Process group cleanup failed: {exc}")
    if process is not None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"Process cleanup failed: {exc}")
        try:
            await asyncio.wait_for(
                asyncio.gather(process.wait(), *readers), _CLEANUP_TIMEOUT,
            )
        except (TimeoutError, OSError) as exc:
            errors.append(f"Process/pipe cleanup incomplete: {type(exc).__name__}: {exc}")
            # Public Process has no pipe-close method. Release inherited pipe
            # transports on a failed cleanup instead of leaving the event loop hung.
            process._transport.close()
    return errors


async def run_extension(
    extension_id: str,
    script: str = "smoke.py",
    arguments: list[str] | None = None,
    *,
    timeout: float = 30,
) -> dict:
    """Return script diagnostics, not a semantic certificate or a side-effect undo.

    Cancellation is propagated only after tree cleanup and last-report persistence.
    The generated smoke script asserts SkillResult.success; arbitrary scripts must
    define their own assertions. No retry or dependency installation is performed.
    """
    root = store.extension_root(extension_id)
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (float, int))
        or not 0 < timeout <= MAX_TIMEOUT
    ):
        raise store.ExtensionError("invalid_timeout", "timeout must be greater than zero and at most 60 seconds")
    if arguments is None:
        arguments = []
    if not isinstance(arguments, list) or any(
        not isinstance(argument, str) or "\x00" in argument for argument in arguments
    ):
        raise store.ExtensionError("invalid_arguments", "arguments must be a list of strings without NUL")
    _script_path(root / "draft", script)
    report = {
        "extension_id": extension_id,
        "script": script,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "interrupted": False,
        "output_truncated": False,
        "execution_started": False,
        "success": False,
        "success_scope": "Script completion only; arbitrary scripts need their own semantic assertions.",
        "side_effects": "Possible host/network effects; disposable copies are not a sandbox or an undo.",
        "state_change_unknown": False,
        "cleanup_errors": [],
    }
    started = time.monotonic()
    cancellation = None
    workspace = TemporaryDirectory(prefix=".run-", dir=root)
    try:
        work = Path(workspace.name)
        package, data, scratch = work / "package", work / "data", work / "scratch"
        store.copy_package(root / "draft", package)
        data.mkdir()
        scratch.mkdir()
        entry = _script_path(package, script)
        process = job = completion = None
        readers = []
        stdout, stderr, truncated = bytearray(), bytearray(), [False]
        try:
            if os.name == "nt":
                job = _WindowsJob()
            launch = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-P", "-s", "-u", "-m", "mochi.extensions.worker",
                str(entry), *arguments,
                cwd=package,
                env=_environment(data, scratch),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
            ))
            # A cancelled subprocess creation may already have spawned Python.
            # Retain and reap that process before propagating cancellation.
            try:
                process = await asyncio.shield(launch)
            except asyncio.CancelledError as exc:
                cancellation = exc
                report["interrupted"] = True
                while not launch.done():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(launch)
                process = launch.result()
                raise
            if job is not None:
                job.assign(process.pid)
            readers = [
                asyncio.create_task(_drain(process.stdout, stdout, truncated)),
                asyncio.create_task(_drain(process.stderr, stderr, truncated)),
            ]
            process.stdin.write(b"1")
            report["execution_started"] = True
            await process.stdin.drain()
            process.stdin.close()
            completion = asyncio.gather(process.wait(), *readers)
            try:
                await asyncio.wait_for(asyncio.shield(completion), timeout)
            except TimeoutError:
                report["timed_out"] = True
        except asyncio.CancelledError as exc:
            report["interrupted"] = True
            cancellation = exc
        except (OSError, RuntimeError) as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            cleanup = asyncio.create_task(_cleanup(process, job, readers))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as exc:
                    cancellation = exc
                    report["interrupted"] = True
            report["cleanup_errors"].extend(cleanup.result())
            if completion is not None:
                if not completion.done():
                    completion.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError):
                    await completion
            if process is not None:
                report["exit_code"] = process.returncode
            report["stdout"] = stdout.decode("utf-8", errors="replace")
            report["stderr"] = stderr.decode("utf-8", errors="replace")
            report["output_truncated"] = truncated[0]
    finally:
        try:
            workspace.cleanup()
        except OSError as exc:
            report["cleanup_errors"].append(f"Disposable files cleanup failed: {exc}")
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["state_change_unknown"] = report["execution_started"]
    report["success"] = bool(
        report["execution_started"] and report["exit_code"] == 0
        and not report["timed_out"] and not report["interrupted"]
        and not report["cleanup_errors"] and "error" not in report
    )
    report["report_path"] = str(root / "last-run.json")
    report["report_saved"] = True
    try:
        report["report_path"] = str(_write_run_report(extension_id, report))
    except (OSError, store.ExtensionError) as exc:
        report["report_saved"] = False
        report["report_error"] = f"Last-run report could not be saved: {exc}"
    if cancellation is not None:
        raise cancellation
    return report
