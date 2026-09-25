"""One official-release updater shared by Main, Admin, and the launcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from threading import Lock
import uuid

import httpx

from mochi._version import read_version

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OFFICIAL_REPOSITORY = "shikidmsh-rgb/mochibot"
OFFICIAL_REMOTE_URL = f"https://github.com/{OFFICIAL_REPOSITORY}.git"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{OFFICIAL_REPOSITORY}/releases/latest"
UPDATE_REQUEST_PATH = PROJECT_ROOT / "data" / ".update_request"
UPDATE_RESULT_PATH = PROJECT_ROOT / "data" / ".update_result"
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERSION_RE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
_update_lock = Lock()
_active_request_id: str | None = None

_ERRORS = {
    "release_unavailable": ("读取 GitHub 官方 Release 失败，请稍后重试。", True),
    "invalid_release": ("GitHub 最新 Release 不是有效的正式版本，未准备更新。", False),
    "invalid_local_version": ("本地版本号无效，无法比较官方版本。", False),
    "container_update_unsupported": ("容器安装请由 Docker 更新镜像，Mochi 不会在容器内改写自己。", False),
    "update_launcher_required": ("当前启动方式不支持自助更新，请由用户通过 scripts/start.py 启动后再试。", False),
    "not_git_installation": ("当前不是 Git 安装，无法自动更新。", False),
    "git_inspection_failed": ("无法确认当前 Git 工作区状态，未准备更新。", True),
    "dirty_worktree": ("检测到本地代码改动，未准备更新；请用户先处理这些改动。", False),
    "official_fetch_failed": ("获取官方 Release 代码失败，未修改当前代码。", True),
    "release_version_mismatch": ("Release 标签与代码版本不一致，未修改当前代码。", False),
    "non_fast_forward": ("当前代码无法安全快进到该 Release，未覆盖本地历史。", False),
    "protected_data_paths": ("该 Release 涉及本地配置或数据路径，已拒绝自动更新。", False),
    "update_pending": ("已有官方更新请求等待当前回复送达，未替换该请求。", False),
    "update_stage_failed": ("无法保存更新请求，未开始安装或重启。", True),
}


class UpdateError(RuntimeError):
    def __init__(
        self, code: str, message: str, retryable: bool = False,
        code_updated: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.code_updated = code_updated


def _error(code: str) -> UpdateError:
    message, retryable = _ERRORS[code]
    return UpdateError(code, message, retryable=retryable)


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    name: str
    notes: str
    url: str
    current_version: str
    available: bool
    notes_truncated: bool = False


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    normalized = value.strip()
    match = _TAG_RE.fullmatch(normalized if normalized.startswith("v") else f"v{normalized}")
    return tuple(int(part) for part in match.groups()) if match else None


def _run_git(*args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(PROJECT_ROOT), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Update Git command failed: %s", args[0])
        return 1, ""
    output = result.stdout if result.returncode == 0 else (result.stdout or "") + (result.stderr or "")
    return result.returncode, (output or "").strip()


def _git_output(*args: str, code: str = "git_inspection_failed", timeout: int = 60) -> str:
    status, output = _run_git(*args, timeout=timeout)
    if status != 0:
        raise _error(code)
    return output


def _is_container() -> bool:
    return (Path(os.path.sep) / ".dockerenv").exists() or bool(os.getenv("KUBERNETES_SERVICE_HOST"))


def validate_installation(
    *, require_clean: bool = True, require_launcher: bool = True,
) -> dict:
    if _is_container():
        raise _error("container_update_unsupported")
    if require_launcher and os.getenv("MOCHIBOT_UPDATE_LAUNCHER") != "1":
        raise _error("update_launcher_required")
    if not (PROJECT_ROOT / ".git").exists():
        raise _error("not_git_installation")
    if _git_output("rev-parse", "--is-inside-work-tree") != "true":
        raise _error("not_git_installation")
    head = _git_output("rev-parse", "HEAD")
    if not _HASH_RE.fullmatch(head):
        raise _error("git_inspection_failed")
    dirty = bool(_git_output("status", "--porcelain", "--untracked-files=normal"))
    if require_clean and dirty:
        raise _error("dirty_worktree")
    return {
        "commit": head, "branch": _git_output("branch", "--show-current"),
        "dirty": dirty,
    }


async def check_for_update() -> ReleaseInfo:
    current_version = read_version()
    current = _version_tuple(current_version)
    if current is None:
        raise _error("invalid_local_version")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(LATEST_RELEASE_URL, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"MochiBot/{current_version}",
                "X-GitHub-Api-Version": "2022-11-28",
            })
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        log.exception("Official release lookup failed")
        raise _error("release_unavailable") from None
    if not isinstance(payload, dict):
        raise _error("invalid_release")
    tag = payload.get("tag_name")
    if (
        not isinstance(tag, str) or not _TAG_RE.fullmatch(tag)
        or payload.get("draft") is not False
        or payload.get("prerelease") is not False
    ):
        raise _error("invalid_release")
    latest = _version_tuple(tag)
    notes = str(payload.get("body") or "").strip()
    return ReleaseInfo(
        tag=tag, version=tag[1:], name=str(payload.get("name") or tag),
        notes=notes[:4000],
        url=f"https://github.com/{OFFICIAL_REPOSITORY}/releases/tag/{tag}",
        current_version=current_version, available=latest > current,
        notes_truncated=len(notes) > 4000,
    )


def _validate_target(target: str, version: str, head: str) -> None:
    if not _HASH_RE.fullmatch(target):
        raise _error("git_inspection_failed")
    code_version = _git_output("show", f"{target}:mochi/__init__.py")
    match = _VERSION_RE.search(code_version)
    if not match or match.group(1) != version:
        raise _error("release_version_mismatch")
    if _git_output("ls-tree", "-r", "--name-only", target, "--", ".env", "data"):
        raise _error("protected_data_paths")
    status, _ = _run_git("merge-base", "--is-ancestor", head, target)
    if status == 1:
        raise _error("non_fast_forward")
    if status:
        raise _error("git_inspection_failed")


def _prepare_update(release: ReleaseInfo) -> dict:
    with _update_lock:
        if (
            not release.available or not _TAG_RE.fullmatch(release.tag)
            or release.tag[1:] != release.version
        ):
            raise _error("invalid_release")
        installation = validate_installation()
        _git_output(
            "fetch", "--no-tags", OFFICIAL_REMOTE_URL, f"refs/tags/{release.tag}",
            code="official_fetch_failed", timeout=120,
        )
        target = _git_output("rev-parse", "--verify", "FETCH_HEAD^{commit}")
        _validate_target(target, release.version, installation["commit"])
        return {
            "tag": release.tag, "version": release.version,
            "current_version": release.current_version,
            "pre_head": installation["commit"], "target_commit": target,
        }


async def prepare_update(release: ReleaseInfo) -> dict:
    return await asyncio.to_thread(_prepare_update, release)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    try:
        pending.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Invalid update record")
    return payload


def stage_update(
    prepared: dict, *, user_id: int, channel_id: int, transport: str, turn_id: str,
) -> dict:
    global _active_request_id
    with _update_lock:
        try:
            previous = _read_json(UPDATE_REQUEST_PATH)
            if previous and previous.get("request_id") == _active_request_id:
                if (
                    previous.get("target_commit") == prepared.get("target_commit")
                    and previous.get("user_id") == user_id
                    and previous.get("channel_id") == channel_id
                    and previous.get("transport") == transport
                ):
                    if previous.get("turn_id") == turn_id:
                        return {**previous, "changed": False}
                    # A fresh explicit request in the same conversation can
                    # retry delivery without replacing the prepared release.
                    previous["turn_id"] = turn_id
                    _write_json(UPDATE_REQUEST_PATH, previous)
                    return {**previous, "changed": True}
                raise _error("update_pending")
            request = {
                **prepared, "request_id": uuid.uuid4().hex,
                "user_id": user_id, "channel_id": channel_id,
                "transport": transport, "turn_id": turn_id,
            }
            if transport == "wechat":
                from mochi.db import get_skill_config

                owner_id = get_skill_config("_transport:wechat").get("owner_weixin_id")
                if owner_id:
                    request["weixin_id"] = owner_id
            _write_json(UPDATE_REQUEST_PATH, request)
            _active_request_id = request["request_id"]
            return {**request, "changed": True}
        except (OSError, ValueError):
            log.exception("Could not stage official update")
            raise _error("update_stage_failed") from None


def request_update_exit(request_id: str) -> None:
    from mochi.shutdown import UPDATE_EXIT_CODE, request_process_exit

    with _update_lock:
        request = _read_json(UPDATE_REQUEST_PATH)
        if request is None or request.get("request_id") != request_id:
            raise _error("update_stage_failed")
        request_process_exit(UPDATE_EXIT_CODE)


def peek_update_result() -> dict | None:
    return _read_json(UPDATE_RESULT_PATH)


def ack_update_result(request_id: str) -> bool:
    with _update_lock:
        result = _read_json(UPDATE_RESULT_PATH)
        if not result or result.get("request_id") != request_id:
            return False
        UPDATE_RESULT_PATH.unlink()
        return True


def _sync_requirements(python_executable: str) -> bool:
    try:
        result = subprocess.run(
            [python_executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=600,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        log.exception("Update requirements synchronization failed")
        return False


def _apply_request(request: dict, python_executable: str) -> dict:
    tag = request.get("tag")
    version = request.get("version")
    target = request.get("target_commit")
    before = request.get("pre_head")
    if not (
        isinstance(tag, str) and _TAG_RE.fullmatch(tag)
        and tag[1:] == version
        and isinstance(target, str) and _HASH_RE.fullmatch(target)
        and isinstance(before, str) and _HASH_RE.fullmatch(before)
    ):
        return {"ok": False, "code_updated": False, "message": "更新未执行：更新请求无效，未修改代码。"}
    try:
        installation = validate_installation()
        if installation["commit"] != before:
            return {
                "ok": False, "code_updated": False,
                "message": "更新未执行：准备更新后本地代码发生变化，未覆盖这些改动。",
            }
        _validate_target(target, version, before)
        requirements_changed = bool(_git_output(
            "diff", "--name-only", before, target, "--", "requirements.txt",
        ))
    except UpdateError as exc:
        return {"ok": False, "code_updated": False, "message": f"更新未执行：{exc}"}

    status, _ = _run_git("merge", "--ff-only", "--no-overwrite-ignore", target, timeout=120)
    head_status, after = _run_git("rev-parse", "HEAD")
    changed = head_status == 0 and after != before
    if status or head_status or after != target:
        return {
            "ok": False, "code_updated": changed,
            "state_change_unknown": head_status != 0,
            "message": "更新失败：无法完成官方 Release 的快进安装。请用户检查本地 Git 状态。",
        }
    if requirements_changed and not _sync_requirements(python_executable):
        return {
            "ok": False, "code_updated": changed, "version": version,
            "message": f"代码已更新到官方正式版 v{version}，但依赖安装失败；未报告为更新完成。请用户检查运行环境。",
        }
    try:
        for pycache in (PROJECT_ROOT / "mochi").rglob("__pycache__"):
            shutil.rmtree(pycache)
    except OSError:
        log.exception("Updated code bytecode cleanup failed")
        return {
            "ok": False, "code_updated": changed, "version": version,
            "message": f"代码已更新到官方正式版 v{version}，但更新收尾失败；未报告为更新完成。请用户检查运行环境。",
        }
    return {
        "ok": True, "code_updated": changed, "version": version,
        "message": f"已经更新到官方正式版 v{version}，重新上线啦。",
    }


def apply_pending_update(python_executable: str = sys.executable) -> dict | None:
    with _update_lock:
        try:
            request = _read_json(UPDATE_REQUEST_PATH)
        except (OSError, ValueError):
            log.exception("Invalid staged update record")
            return {
                "ok": False, "code_updated": False,
                "message": "更新未执行：更新请求无效，未修改代码。",
            }
        if request is None:
            return None
        try:
            result = _apply_request(request, python_executable)
        except Exception:
            log.exception("Official update interrupted")
            result = {
                "ok": False, "state_change_unknown": True,
                "message": "更新流程异常中断，安装状态尚未确认；请用户检查运行环境。",
            }
        payload = {
            key: request.get(key, "")
            for key in ("request_id", "user_id", "channel_id", "transport", "weixin_id")
        }
        payload.update(result)
        _write_json(UPDATE_RESULT_PATH, payload)
        UPDATE_REQUEST_PATH.unlink()
        return payload


def main() -> int:
    result = apply_pending_update()
    if result:
        print(result["message"])
    return 0 if result is None or result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
