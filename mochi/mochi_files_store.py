"""Bounded private Markdown storage for works authored by Main."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ACTIVE_DIRNAME = "mochi_files"
PREVIOUS_DIRNAME = ".mochi_files_previous"
LOCK_FILENAME = ".mochi_files.lock"

MAX_ACTIVE_FILES = 100
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_LIST_RESULTS = 100
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_QUERY_CHARS = 512
MAX_SEARCH_EXCERPT_CHARS = 240
MAX_READ_CHARS = 16 * 1024
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05

_WINDOWS_INVALID_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x0400,
)
_thread_lock = threading.RLock()


class MochiFilesError(ValueError):
    """Base error for rejected Mochi Files operations."""

    code = "storage_error"
    retryable = False


class InvalidPathError(MochiFilesError):
    code = "invalid_path"


class InvalidArgumentsError(MochiFilesError):
    code = "invalid_arguments"


class FileMissingError(MochiFilesError):
    code = "not_found"


class FileConflictError(MochiFilesError):
    code = "conflict"
    retryable = True


class QuotaExceededError(MochiFilesError):
    code = "quota_exceeded"


class LockTimeoutError(MochiFilesError):
    code = "lock_timeout"
    retryable = True


class StorageIOError(MochiFilesError):
    code = "storage_io_error"


def _active_root() -> Path:
    return DATA_DIR / ACTIVE_DIRNAME


def _previous_root() -> Path:
    return DATA_DIR / PREVIOUS_DIRNAME


def _lock_path() -> Path:
    return DATA_DIR / LOCK_FILENAME


def _contains_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_components(path: str, *, require_markdown: bool) -> tuple[str, ...]:
    if not isinstance(path, str) or not path:
        raise InvalidPathError("path must be a nonempty relative POSIX path")
    if _contains_control_characters(path):
        raise InvalidPathError("path must not contain control characters")
    if "\\" in path:
        raise InvalidPathError("path must use POSIX '/' separators, not backslashes")
    if path.startswith("/") or ":" in path:
        raise InvalidPathError("absolute paths and drive/colon forms are not allowed")

    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise InvalidPathError("path contains an empty, current, or parent component")
    for part in parts:
        if part.startswith("."):
            raise InvalidPathError("hidden path components are not allowed")
        if any(character in _WINDOWS_INVALID_CHARS for character in part):
            raise InvalidPathError("path contains characters invalid on Windows")
        if part.endswith((" ", ".")):
            raise InvalidPathError("path components must not end with a space or dot")
        if part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise InvalidPathError("path contains a reserved Windows device name")
    if require_markdown and not parts[-1].endswith(".md"):
        raise InvalidPathError("Mochi Files paths must end with lowercase .md")
    return parts


def _file_parts(path: str) -> tuple[str, ...]:
    return _validate_components(path, require_markdown=True)


def _scope_parts(path: str | None) -> tuple[str, ...]:
    if path is None:
        return ()
    return _validate_components(path, require_markdown=False)


def _relative_posix(parts: tuple[str, ...]) -> str:
    return "/".join(parts)


def _assert_root_safe(root: Path) -> None:
    if not root.exists():
        return
    info = root.lstat()
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise InvalidPathError(f"storage root is not a regular directory: {root.name}")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _assert_existing_chain_safe(root: Path, parts: tuple[str, ...]) -> None:
    _assert_root_safe(root)
    current = root
    for index, part in enumerate(parts):
        current = current / part
        if not current.exists() and not current.is_symlink():
            return
        info = current.lstat()
        if _is_link_or_reparse(info):
            raise InvalidPathError(
                "symbolic links and reparse points are not allowed in Mochi Files paths"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InvalidPathError("a path parent is not a directory")


def _ensure_parent_safe(root: Path, parent_parts: tuple[str, ...]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _assert_root_safe(root)
    current = root
    for part in parent_parts:
        current = current / part
        try:
            current.mkdir()
        except FileExistsError:
            pass
        info = current.lstat()
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise InvalidPathError("a path parent is not a regular directory")
    _assert_existing_chain_safe(root, parent_parts)
    return current


def _read_bytes(root: Path, parts: tuple[str, ...]) -> bytes:
    _assert_existing_chain_safe(root, parts)
    path = root.joinpath(*parts)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise FileMissingError(f"file not found: {_relative_posix(parts)}") from exc
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise InvalidPathError("target must be a regular file, not a link or directory")
    try:
        with path.open("rb") as handle:
            return handle.read()
    except OSError as exc:
        raise StorageIOError(f"could not read {_relative_posix(parts)}: {exc}") from exc


def _decode_utf8(content: bytes, path: str) -> str:
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise StorageIOError(f"{path} is not valid UTF-8") from exc


def _encode_utf8(content: object, field: str = "content") -> bytes:
    if not isinstance(content, str):
        raise InvalidArgumentsError(f"{field} must be a string")
    try:
        return content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise InvalidArgumentsError(f"{field} must be valid UTF-8 text") from exc


def _iter_regular_files(root: Path) -> list[tuple[str, Path, int]]:
    _assert_root_safe(root)
    if not root.exists():
        return []
    found: list[tuple[str, Path, int]] = []

    def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise StorageIOError(f"could not scan {root.name}: {exc}") from exc
        for entry in entries:
            parts = relative_parts + (entry.name,)
            try:
                info = entry.stat(follow_symlinks=False)
                if _is_link_or_reparse(info):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), parts)
                elif entry.is_file(follow_symlinks=False):
                    found.append(
                        (_relative_posix(parts), Path(entry.path), info.st_size)
                    )
            except OSError as exc:
                raise StorageIOError(
                    f"could not inspect {_relative_posix(parts)}: {exc}"
                ) from exc

    walk(root, ())
    return sorted(found, key=lambda item: item[0])


def _active_markdown_files() -> list[tuple[str, Path, int]]:
    return [
        item for item in _iter_regular_files(_active_root())
        if item[0].endswith(".md")
        and all(not part.startswith(".") for part in item[0].split("/"))
    ]


def _storage_sizes() -> tuple[int, dict[str, int], dict[str, int]]:
    active = {path: size for path, _, size in _iter_regular_files(_active_root())}
    previous = {
        path: size for path, _, size in _iter_regular_files(_previous_root())
    }
    return sum(active.values()) + sum(previous.values()), active, previous


def _validate_file_limit(content: bytes) -> None:
    if len(content) > MAX_FILE_BYTES:
        raise QuotaExceededError(
            f"file would be {len(content)} UTF-8 bytes; limit is {MAX_FILE_BYTES}"
        )


def _validate_projected_quotas(
    relative_path: str,
    new_content: bytes,
    *,
    backup_content: bytes | None,
) -> None:
    _validate_file_limit(new_content)
    total, active, previous = _storage_sizes()
    current_size = active.get(relative_path, 0)
    previous_size = previous.get(relative_path, 0)
    if relative_path not in active and len(active) >= MAX_ACTIVE_FILES:
        raise QuotaExceededError(
            f"active file limit of {MAX_ACTIVE_FILES} has been reached"
        )
    projected = total - current_size + len(new_content)
    if backup_content is not None:
        projected = projected - previous_size + len(backup_content)
    if projected > MAX_TOTAL_BYTES:
        raise QuotaExceededError(
            f"write would use {projected} bytes across active and previous files; "
            f"combined limit is {MAX_TOTAL_BYTES} bytes"
        )


def _write_temp(path: Path, content: bytes) -> str:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return temp_name
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_replace(path: Path, content: bytes) -> None:
    temp_name = _write_temp(path, content)
    try:
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_create(path: Path, content: bytes) -> None:
    temp_name = _write_temp(path, content)
    try:
        os.link(temp_name, path)
    except FileExistsError as exc:
        raise FileConflictError(f"file already exists: {path.name}") from exc
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _interprocess_lock(timeout: float | None = None):
    timeout = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path()
    handle = path.open("a+b")
    deadline = time.monotonic() + max(0.0, timeout)
    acquired = False
    try:
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"timed out after {timeout:.1f}s waiting for Mochi Files lock"
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _transaction():
    with _thread_lock:
        with _interprocess_lock():
            yield


def _bounded_page(offset: object, limit: object, maximum: int) -> tuple[int, int]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidArgumentsError("offset must be a nonnegative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise InvalidArgumentsError(f"limit must be an integer from 1 to {maximum}")
    return offset, limit


def list_files(
    *,
    path: str | None = None,
    offset: int = 0,
    limit: int = MAX_LIST_RESULTS,
) -> dict:
    offset, limit = _bounded_page(offset, limit, MAX_LIST_RESULTS)
    scope = _scope_parts(path)
    with _transaction():
        root = _active_root()
        _assert_existing_chain_safe(root, scope)
        if scope:
            scope_path = root.joinpath(*scope)
            if not scope_path.exists():
                raise FileMissingError(f"directory not found: {_relative_posix(scope)}")
            info = scope_path.lstat()
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise InvalidPathError("list path must name a regular directory")
        prefix = f"{_relative_posix(scope)}/" if scope else ""
        files = [
            {"path": relative, "bytes": size}
            for relative, _, size in _active_markdown_files()
            if relative.startswith(prefix)
        ]
    page = files[offset:offset + limit]
    next_offset = offset + len(page)
    complete = next_offset >= len(files)
    return {
        "action": "list",
        "scope": _relative_posix(scope),
        "offset": offset,
        "count": len(page),
        "total": len(files),
        "files": page,
        "complete": complete,
        "next_offset": None if complete else next_offset,
    }


def _search_scope(path: str | None) -> tuple[tuple[str, ...], bool]:
    parts = _scope_parts(path)
    return parts, bool(parts and parts[-1].endswith(".md"))


def search_files(
    query: str,
    *,
    path: str | None = None,
    offset: int = 0,
    limit: int = MAX_SEARCH_RESULTS,
) -> dict:
    if not isinstance(query, str) or not query:
        raise InvalidArgumentsError("query must be a nonempty literal string")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise InvalidArgumentsError(
            f"query must be at most {MAX_SEARCH_QUERY_CHARS} characters"
        )
    offset, limit = _bounded_page(offset, limit, MAX_SEARCH_RESULTS)
    scope, file_scope = _search_scope(path)
    scope_text = _relative_posix(scope)
    matches: list[dict] = []
    total_matches = 0
    with _transaction():
        root = _active_root()
        _assert_existing_chain_safe(root, scope)
        if scope:
            scope_path = root.joinpath(*scope)
            if not scope_path.exists():
                raise FileMissingError(f"search scope not found: {scope_text}")
            info = scope_path.lstat()
            expected = stat.S_ISREG(info.st_mode) if file_scope else stat.S_ISDIR(info.st_mode)
            if _is_link_or_reparse(info) or not expected:
                raise InvalidPathError("search path has the wrong file/directory form")
        prefix = f"{scope_text}/" if scope and not file_scope else ""
        candidates = [
            item for item in _active_markdown_files()
            if (item[0] == scope_text if file_scope else item[0].startswith(prefix))
        ]
        for relative, _, _ in candidates:
            text = _decode_utf8(
                _read_bytes(root, tuple(relative.split("/"))),
                relative,
            )
            start = 0
            while True:
                match_start = text.find(query, start)
                if match_start < 0:
                    break
                excerpt_start = max(0, match_start - MAX_SEARCH_EXCERPT_CHARS // 2)
                excerpt_end = min(
                    len(text),
                    excerpt_start + MAX_SEARCH_EXCERPT_CHARS,
                )
                if excerpt_end - excerpt_start < MAX_SEARCH_EXCERPT_CHARS:
                    excerpt_start = max(0, excerpt_end - MAX_SEARCH_EXCERPT_CHARS)
                if offset <= total_matches < offset + limit:
                    matches.append({
                        "path": relative,
                        "match_start": match_start,
                        "match_end": match_start + len(query),
                        "excerpt_start": excerpt_start,
                        "excerpt": text[excerpt_start:excerpt_end],
                    })
                total_matches += 1
                start = match_start + len(query)
    next_offset = offset + len(matches)
    complete = next_offset >= total_matches
    return {
        "action": "search",
        "scope": scope_text,
        "query": query,
        "offset": offset,
        "count": len(matches),
        "total_matches": total_matches,
        "matches": matches,
        "complete": complete,
        "next_offset": None if complete else next_offset,
    }


def read_file(
    path: str,
    *,
    offset: int = 0,
    limit: int = MAX_READ_CHARS,
) -> dict:
    parts = _file_parts(path)
    offset, limit = _bounded_page(offset, limit, MAX_READ_CHARS)
    relative = _relative_posix(parts)
    with _transaction():
        content_bytes = _read_bytes(_active_root(), parts)
        text = _decode_utf8(content_bytes, relative)
    end = min(len(text), offset + limit)
    content = text[offset:end] if offset < len(text) else ""
    complete = end >= len(text)
    return {
        "action": "read",
        "path": relative,
        "bytes": len(content_bytes),
        "total_chars": len(text),
        "offset": offset,
        "end_offset": end,
        "content": content,
        "complete": complete,
        "next_offset": None if complete else end,
    }


def create_file(path: str, content: str) -> dict:
    parts = _file_parts(path)
    relative = _relative_posix(parts)
    encoded = _encode_utf8(content)
    with _transaction():
        _ensure_parent_safe(_active_root(), parts[:-1])
        _assert_existing_chain_safe(_active_root(), parts)
        target = _active_root().joinpath(*parts)
        if target.exists() or target.is_symlink():
            raise FileConflictError(f"file already exists: {relative}")
        _validate_projected_quotas(relative, encoded, backup_content=None)
        try:
            _atomic_create(target, encoded)
        except MochiFilesError:
            raise
        except OSError as exc:
            raise StorageIOError(f"could not create {relative}: {exc}") from exc
    return {"action": "create", "path": relative, "bytes": len(encoded)}


def _mutate_file(
    action: str,
    path: str,
    transform,
) -> dict:
    parts = _file_parts(path)
    relative = _relative_posix(parts)
    with _transaction():
        current = _read_bytes(_active_root(), parts)
        current_text = _decode_utf8(current, relative)
        new_text = transform(current_text)
        encoded = _encode_utf8(new_text)
        _validate_projected_quotas(relative, encoded, backup_content=current)

        _ensure_parent_safe(_previous_root(), parts[:-1])
        previous_target = _previous_root().joinpath(*parts)
        target = _active_root().joinpath(*parts)
        try:
            _atomic_replace(previous_target, current)
        except OSError as exc:
            raise StorageIOError(
                f"could not save previous version for {relative}: {exc}"
            ) from exc
        try:
            _atomic_replace(target, encoded)
        except OSError as exc:
            raise StorageIOError(f"could not update {relative}: {exc}") from exc
    return {
        "action": action,
        "path": relative,
        "bytes": len(encoded),
        "previous_bytes": len(current),
    }


def append_file(path: str, content: str) -> dict:
    addition = _encode_utf8(content)
    addition_text = _decode_utf8(addition, "content")
    return _mutate_file("append", path, lambda current: current + addition_text)


def edit_file(path: str, old_text: str, new_text: str) -> dict:
    _encode_utf8(old_text, "old_text")
    _encode_utf8(new_text, "new_text")
    if not old_text:
        raise InvalidArgumentsError("old_text must be nonempty")

    def replace_once(current: str) -> str:
        first = current.find(old_text)
        second = current.find(old_text, first + 1) if first >= 0 else -1
        if first < 0 or second >= 0:
            count = 0 if first < 0 else 2
            raise FileConflictError(
                f"old_text must occur exactly once; found {count} "
                f"{'matches' if count != 2 else 'or more matches'}"
            )
        return current[:first] + new_text + current[first + len(old_text):]

    return _mutate_file("edit", path, replace_once)
