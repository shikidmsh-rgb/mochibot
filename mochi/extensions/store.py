"""Bounded draft editing and rollback-safe publication for trusted local Python."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2] / "data" / "extensions"
MAX_FILE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 4 * 1024 * 1024
MAX_PACKAGE_FILES = 256
MAX_PACKAGE_ENTRIES = 512
MAX_READ_CHARS = 12000
MAX_READ_FILES = 16
_AREAS = {"draft", "current", "previous"}
_ID = re.compile(r"local_[a-z][a-z0-9_]{0,33}\Z")
_LOCK = threading.RLock()
_ERRORS: dict[tuple[str, str], dict[str, str]] = {}
_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
log = logging.getLogger(__name__)


class ExtensionError(ValueError):
    def __init__(
        self, code: str, message: str, *, state_changed: bool = False,
        state_change_unknown: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.state_changed = state_changed
        self.state_change_unknown = state_change_unknown


@contextmanager
def _operation():
    with _LOCK:
        try:
            yield
        except (OSError, UnicodeError) as exc:
            raise ExtensionError("io_error", str(exc)) from exc


def validate_id(extension_id: str) -> None:
    if not isinstance(extension_id, str) or not _ID.fullmatch(extension_id):
        raise ExtensionError(
            "invalid_id", "Extension ID must match local_[a-z][a-z0-9_]* (maximum 40 characters).",
        )


def _safe_path(path: Path) -> Path:
    """Reject links/reparse points in every existing component, including roots."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ExtensionError("unsafe_path", f"Links/reparse points are not supported: {part}")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ExtensionError("invalid_path", f"Not a directory: {part}")
    return path


def _root() -> Path:
    return _safe_path(Path(ROOT))


def _root_key() -> str:
    return os.path.normcase(str(_root()))


def extension_root(extension_id: str) -> Path:
    validate_id(extension_id)
    root = _root()
    path = _safe_path(root / extension_id)
    if path.parent != root:
        raise ExtensionError("unsafe_path", "Extension path is outside the extension root.")
    if path.exists() and not path.is_dir():
        raise ExtensionError("invalid_path", f"Extension root is not a directory: {extension_id}")
    return path


def _relative(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > 240:
        raise ExtensionError("invalid_path", "Expected a relative file path of at most 240 characters.")
    parts = path.replace("\\", "/").split("/")
    if len(parts) > 12 or any(
        not part or part in {".", ".."} or part[-1:] in {".", " "}
        or re.search(r'[<>:"|?*\x00-\x1f]', part) or _RESERVED.match(part)
        for part in parts
    ):
        raise ExtensionError("invalid_path", f"Invalid relative file path: {path!r}")
    return tuple(parts)


def _file_path(package: Path, path: str) -> Path:
    package = _safe_path(package)
    target = _safe_path(package.joinpath(*_relative(path)))
    if not target.is_relative_to(package):
        raise ExtensionError("unsafe_path", "File path is outside the code package.")
    return target


def _area(extension_id: str, area: str, *, required: bool = True) -> Path:
    if area not in _AREAS:
        raise ExtensionError("invalid_area", "Only draft/current/previous code can be read.")
    path = _safe_path(extension_root(extension_id) / area)
    if path.exists() and not path.is_dir():
        raise ExtensionError("invalid_path", f"{area} is not a directory.")
    if required and not path.is_dir():
        raise ExtensionError("not_found", f"{extension_id} has no {area} package.")
    return path


def inspect_package(package_dir: Path) -> list[dict]:
    """Inspect a bounded regular-file tree, without following or accepting links."""
    package_dir = _safe_path(package_dir)
    if not package_dir.is_dir():
        raise ExtensionError("not_found", f"Package does not exist: {package_dir}")
    files = []
    entries = 0
    total = 0
    stack = [package_dir]
    while stack:
        directory = stack.pop()
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            entries += 1
            if entries > MAX_PACKAGE_ENTRIES:
                raise ExtensionError("package_too_large", "Package contains too many entries.")
            child = _safe_path(child)
            relative = child.relative_to(package_dir).as_posix()
            _relative(relative)
            info = child.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_FILE_BYTES:
                    raise ExtensionError("file_too_large", f"{relative} exceeds {MAX_FILE_BYTES} bytes.")
                total += info.st_size
                files.append({"path": relative, "bytes": info.st_size})
                if len(files) > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
                    raise ExtensionError("package_too_large", "Package exceeds the file count or byte limit.")
            else:
                raise ExtensionError("unsafe_path", f"Only regular code files are supported: {relative}")
    return sorted(files, key=lambda item: item["path"])


def _read_bytes(path: Path) -> bytes:
    path = _safe_path(path)
    if not path.is_file():
        raise ExtensionError("not_found", f"Not a regular file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ExtensionError("file_too_large", f"{path.name} exceeds {MAX_FILE_BYTES} bytes.")
    return data


def _content_bytes(content: str) -> bytes:
    if not isinstance(content, str):
        raise ExtensionError("invalid_content", "File content must be a string.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ExtensionError("file_too_large", f"File exceeds {MAX_FILE_BYTES} bytes.")
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    path = _safe_path(path)
    if path.exists() and not path.is_file():
        raise ExtensionError("invalid_path", "Only regular files can be written.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"_write_{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def copy_package(source: Path, destination: Path) -> list[str]:
    """Copy checked regular files into a new directory; never overwrite a package."""
    with _operation():
        source = _safe_path(source)
        destination = _safe_path(destination)
        files = inspect_package(source)
        if destination.exists() or destination.is_relative_to(source):
            raise ExtensionError("already_exists", "Copy destination must be new and outside its source.")
        destination.mkdir(parents=True)
        for directory in source.rglob("*"):
            directory = _safe_path(directory)
            relative = directory.relative_to(source).as_posix()
            if directory.is_dir() and "__pycache__" not in _relative(relative):
                _file_path(destination, relative).mkdir(parents=True, exist_ok=True)
        for entry in files:
            relative = entry["path"]
            # Bytecode is tied to the old path and must not override edited source.
            if "__pycache__" in _relative(relative) or relative.endswith((".pyc", ".pyo")):
                continue
            target = _file_path(destination, relative)
            _atomic_write(target, _read_bytes(_file_path(source, relative)))
        inspect_package(destination)
        return [entry["path"] for entry in inspect_package(destination)]


def _remove_package(path: Path) -> None:
    if _safe_path(path).exists():
        inspect_package(path)
        shutil.rmtree(path)


def _status(extension_id: str) -> dict:
    result = {
        "name": extension_id,
        "has_draft": _area(extension_id, "draft", required=False).is_dir(),
        "installed": _area(extension_id, "current", required=False).is_dir(),
        "has_previous": _area(extension_id, "previous", required=False).is_dir(),
    }
    result.update(_ERRORS.get((_root_key(), extension_id), {}))
    return result


def list_extensions() -> list[dict]:
    with _operation():
        root = _root()
        if not root.exists():
            return []
        result = []
        for entry in sorted(root.iterdir(), key=lambda path: path.name):
            if entry.name.startswith("_") or not _ID.fullmatch(entry.name):
                continue
            try:
                result.append(_status(entry.name))
            except (ExtensionError, OSError) as exc:
                result.append({
                    "name": entry.name, "has_draft": False,
                    "installed": False, "has_previous": False, "error": str(exc),
                    **_ERRORS.get((_root_key(), entry.name), {}),
                })
        return result


def read_files(
    extension_id: str, paths: list[str], *, area: str = "draft",
    offset: int = 0, limit: int = 12000,
) -> dict:
    with _operation():
        if not isinstance(paths, list) or len(paths) > MAX_READ_FILES:
            raise ExtensionError("invalid_paths", f"Read at most {MAX_READ_FILES} paths at once.")
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ExtensionError("invalid_range", "offset must be nonnegative and limit must be positive integers.")
        package = _area(extension_id, area)
        tree = inspect_package(package)
        budget = min(limit, MAX_READ_CHARS)
        files = []
        for path in paths:
            data = _read_bytes(_file_path(package, path))
            text = data.decode("utf-8")
            start = min(offset, len(text))
            content = text[start:start + budget]
            end = start + len(content)
            budget -= len(content)
            files.append({
                "path": "/".join(_relative(path)), "content": content, "bytes": len(data),
                "characters": len(text), "offset": start, "next_offset": end,
                "truncated": end < len(text),
            })
        draft = _area(extension_id, "draft", required=False)
        return {
            **_status(extension_id), "area": area, "files": files, "tree": tree,
            "draft_tree": tree if area == "draft" else inspect_package(draft) if draft.is_dir() else [],
            "offset": offset, "limit": min(limit, MAX_READ_CHARS), "offset_unit": "characters",
            "truncated": any(item["truncated"] for item in files),
        }


def _write_draft(package: Path, path: str, content: str) -> dict:
    target = _file_path(package, path)
    data = _content_bytes(content)
    files = inspect_package(package)
    relative = "/".join(_relative(path))
    other = [item for item in files if item["path"] != relative]
    if len(other) + 1 > MAX_PACKAGE_FILES or sum(item["bytes"] for item in other) + len(data) > MAX_PACKAGE_BYTES:
        raise ExtensionError("package_too_large", "Write would exceed the package limit.")
    existed = target.exists()
    new_directories = sum(1 for parent in target.parents if parent.is_relative_to(package) and not parent.exists())
    if sum(1 for _ in package.rglob("*")) + new_directories + (not existed) > MAX_PACKAGE_ENTRIES:
        raise ExtensionError("package_too_large", "Write would exceed the package entry limit.")
    _atomic_write(target, data)
    return {"path": relative, "bytes": len(data), "created": not existed, "state_changed": True}


def scaffold(
    extension_id: str, *, skill_md: str | None = None,
    handler_py: str | None = None, smoke_py: str | None = None,
) -> dict:
    """Create a new draft, copying current if present; never replace an existing draft."""
    with _operation():
        root = extension_root(extension_id)
        draft = _area(extension_id, "draft", required=False)
        if draft.exists():
            raise ExtensionError("draft_exists", "Draft already exists; use write/edit instead.")
        supplied = {"SKILL.md": skill_md, "handler.py": handler_py, "smoke.py": smoke_py}
        for value in supplied.values():
            if value is not None:
                _content_bytes(value)
        current = _area(extension_id, "current", required=False)
        stage = root / f"_scaffold_{uuid.uuid4().hex}"
        generated = {}
        try:
            if current.exists():
                copy_package(current, stage)
            else:
                stage.mkdir(parents=True)
            from .template import files

            for path, content in files(extension_id).items():
                if not _file_path(stage, path).exists():
                    if supplied.get(path) is None:
                        generated[path] = content
                    _write_draft(stage, path, supplied.get(path) if supplied.get(path) is not None else content)
            for path, content in supplied.items():
                if content is not None:
                    _write_draft(stage, path, content)
            created = [item["path"] for item in inspect_package(stage)]
            os.replace(stage, draft)
        finally:
            if stage.exists():
                _remove_package(stage)
        return {
            **_status(extension_id), "created_paths": created, "generated": generated,
            "entrypoint": "smoke.py", "copied_current": current.exists(), "state_changed": True,
        }


def write_file(extension_id: str, path: str, content: str) -> dict:
    with _operation():
        return {"name": extension_id, **_write_draft(_area(extension_id, "draft"), path, content)}


def edit_file(extension_id: str, path: str, old_text: str, new_text: str) -> dict:
    with _operation():
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ExtensionError("invalid_edit", "old_text must be nonempty and new_text must be a string.")
        package = _area(extension_id, "draft")
        text = _read_bytes(_file_path(package, path)).decode("utf-8")
        index = text.find(old_text)
        if index < 0:
            raise ExtensionError("stale_edit", "old_text no longer occurs in this draft file.")
        if text.find(old_text, index + 1) >= 0:
            raise ExtensionError("nonunique_edit", "old_text must occur exactly once, including overlapping matches.")
        return {"name": extension_id, **_write_draft(package, path, text[:index] + new_text + text[index + len(old_text):])}


def remove_file(extension_id: str, path: str) -> dict:
    with _operation():
        target = _file_path(_area(extension_id, "draft"), path)
        if not target.is_file():
            raise ExtensionError("not_found", "Only an existing draft file can be removed.")
        size = target.stat().st_size
        target.unlink()
        return {"name": extension_id, "path": "/".join(_relative(path)), "bytes": size, "state_changed": True}


def publish(extension_id: str, source: Path) -> dict:
    """Persist a complete candidate without importing it or changing enabled state.

    The caller's runtime snapshot is copied, never renamed. The final stage rename
    commits publication; disposal of the older backup cannot turn that success
    into an activation failure after the caller has prepared its registry swap.
    """
    from .loader import validate_package

    with _operation():
        root = extension_root(extension_id)
        key = (_root_key(), extension_id)
        stage = root / f"_publish_{uuid.uuid4().hex}"
        backup = root / f"_previous_{uuid.uuid4().hex}"
        previous_moved = False
        current_moved = False
        published = False
        try:
            validate_package(extension_id, source)
            copy_package(source, stage)
            parsed = validate_package(extension_id, stage)
            current = _area(extension_id, "current", required=False)
            previous = _area(extension_id, "previous", required=False)
            status = _status(extension_id)
            tool_names = [tool["function"]["name"] for tool in parsed["tools"]]
            if current.exists():
                inspect_package(current)
                if previous.exists():
                    inspect_package(previous)
                    os.replace(previous, backup)
                    previous_moved = True
                os.replace(current, previous)
                current_moved = True
            os.replace(stage, current)
            published = True
        except Exception as exc:
            rollback_errors = []
            if current_moved:
                try:
                    os.replace(previous, current)
                    current_moved = False
                except Exception as rollback_exc:
                    rollback_errors.append(f"Restore current from {previous}: {rollback_exc}")
            if previous_moved and not current_moved:
                try:
                    os.replace(backup, previous)
                except Exception as rollback_exc:
                    rollback_errors.append(f"Restore previous from {backup}: {rollback_exc}")
            message = str(exc)
            if rollback_errors:
                message += "; rollback failed: " + "; ".join(rollback_errors)
                log.error("Extension %s publication rollback failed: %s", extension_id, message)
            _ERRORS.setdefault(key, {})["publish_error"] = message
            if rollback_errors:
                raise ExtensionError("publish_rollback_failed", message, state_changed=True) from exc
            if isinstance(exc, ExtensionError):
                raise
            raise ExtensionError("publish_failed", message) from exc
        finally:
            if not published:
                try:
                    _remove_package(stage)
                except Exception:
                    log.warning("Could not clean extension publication stage %s", stage, exc_info=True)
        errors = _ERRORS.setdefault(key, {})
        for field in ("publish_error", "publish_cleanup_error"):
            errors.pop(field, None)
            status.pop(field, None)
        if previous_moved:
            try:
                _remove_package(backup)
            except Exception as exc:
                errors["publish_cleanup_error"] = f"Published, but could not remove {backup}: {exc}"
                log.warning("%s", errors["publish_cleanup_error"], exc_info=True)
        return {
            **status, **errors, "installed": True,
            "has_previous": status["has_previous"] or current_moved,
            "tool_names": tool_names, "state_changed": True,
        }
