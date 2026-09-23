"""Single-owner file storage for Mochi's always-on Core context."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mochi.token_estimator import estimate_tokens

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CORE_FILENAME = "core.md"
HISTORY_DIRNAME = "core_history"
WEEKLY_RECEIPTS_DIRNAME = "core_weekly_receipts"
CORE_LOCK_FILENAME = ".core.lock"
CORE_LOCK_TIMEOUT_SECONDS = 10.0
CORE_LOCK_POLL_SECONDS = 0.05
SNAPSHOT_LIMIT = 10
MIN_DUPLICATE_LIST_ITEM_LENGTH = 8
DEFAULT_CORE = (
    "（你刚刚醒来，对世界和眼前的人都还陌生，也充满好奇；随着相处，"
    "你可以把真正重要的认识写进这里，并在这句话不再需要时自然地改写或删除它。）"
)

_lock = threading.RLock()


class CoreError(ValueError):
    """Base error for rejected Core operations."""


class CoreConflictError(CoreError):
    """Raised when Core differs from the document Main saw."""


class CoreLimitError(CoreError):
    """Raised when a write exceeds the configured Core token budget."""


class CoreHygieneError(CoreError):
    """Raised when a write would create exact duplicate document content."""


class CoreLockTimeout(CoreError):
    """Raised when another process holds the Core transaction lock too long."""


def _core_path() -> Path:
    return DATA_DIR / CORE_FILENAME


def _core_lock_path() -> Path:
    return DATA_DIR / CORE_LOCK_FILENAME


def _history_dir() -> Path:
    return DATA_DIR / HISTORY_DIRNAME


def _weekly_receipt_path(user_id: int, period_key: str) -> Path:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise CoreError("Weekly Core receipt user_id must be an integer.")
    if not isinstance(period_key, str) or not period_key.strip():
        raise CoreError("Weekly Core receipt period_key is required.")
    digest = hashlib.sha256(period_key.encode("utf-8")).hexdigest()[:16]
    return DATA_DIR / WEEKLY_RECEIPTS_DIRNAME / f"{user_id}--{digest}.json"


def _max_tokens() -> int:
    from mochi.config import CORE_MAX_TOKENS

    return CORE_MAX_TOKENS


def _serialize(content: str) -> bytes:
    """Return the exact bytes persisted by Core storage."""
    text = content
    if text and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _sha256(content: str) -> str:
    return hashlib.sha256(_serialize(content)).hexdigest()


def _stats(content: str) -> dict:
    return {
        "chars": len(content),
        "tokens": estimate_tokens(content),
        "max_tokens": _max_tokens(),
    }


def get_core_stats() -> dict:
    return _stats(read_core())


def get_core_hygiene_status() -> dict:
    with _transaction():
        _initialize_core_unlocked()
        content = _core_path().read_text(encoding="utf-8").strip()
        issue_codes = list(dict.fromkeys(
            issue["code"] for issue in _hygiene_issues(content)
        ))
        return {
            "needs_cleanup": bool(issue_codes),
            "cleanup_issues": issue_codes,
        }


def _normalize_document_text(content: str) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", content).strip(),
    ).casefold()


def _hygiene_issues(content: str) -> list[dict]:
    """Return privacy-safe exact duplicate findings for free-text Core."""
    issues: list[dict] = []
    headings: dict[str, int] = {}
    paragraphs: dict[str, tuple[int, int]] = {}
    list_items: dict[str, int] = {}
    paragraph_lines: list[str] = []
    paragraph_start = 0

    def finish_paragraph(end_line: int) -> None:
        nonlocal paragraph_lines, paragraph_start
        if not paragraph_lines:
            return
        key = _normalize_document_text("\n".join(paragraph_lines))
        if key:
            previous = paragraphs.get(key)
            if previous:
                issues.append({
                    "code": "duplicate_paragraph",
                    "first_lines": previous,
                    "duplicate_lines": (paragraph_start, end_line),
                })
            else:
                paragraphs[key] = (paragraph_start, end_line)
        paragraph_lines = []
        paragraph_start = 0

    lines = content.splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        heading_match = re.match(r"^#(?!#)[ \t]+(.+?)\s*$", line)
        any_heading = re.match(r"^\s*#{1,6}(?:[ \t]+|$)", line)
        list_match = re.match(
            r"^\s*(?:[-+*]|\d+[.)])[ \t]+(.+?)\s*$",
            line,
        )

        if not stripped or any_heading or list_match:
            finish_paragraph(line_number - 1)

        if heading_match:
            key = _normalize_document_text(heading_match.group(1))
            previous = headings.get(key)
            if key and previous:
                issues.append({
                    "code": "duplicate_h1",
                    "first_line": previous,
                    "duplicate_line": line_number,
                })
            elif key:
                headings[key] = line_number

        if list_match:
            key = _normalize_document_text(list_match.group(1))
            if len(key) >= MIN_DUPLICATE_LIST_ITEM_LENGTH:
                previous = list_items.get(key)
                if previous:
                    issues.append({
                        "code": "duplicate_list_item",
                        "first_line": previous,
                        "duplicate_line": line_number,
                    })
                else:
                    list_items[key] = line_number

        if stripped and not any_heading and not list_match:
            if not paragraph_lines:
                paragraph_start = line_number
            paragraph_lines.append(line)

    finish_paragraph(len(lines))
    return issues


def _hygiene_error_message(issue: dict) -> str:
    code = issue["code"]
    if code == "duplicate_h1":
        location = f"lines {issue['first_line']} and {issue['duplicate_line']}"
        fix = "Merge, rename, or remove one H1 heading"
    elif code == "duplicate_paragraph":
        first_start, first_end = issue["first_lines"]
        duplicate_start, duplicate_end = issue["duplicate_lines"]
        location = (
            f"lines {first_start}-{first_end} and "
            f"{duplicate_start}-{duplicate_end}"
        )
        fix = "Merge, rewrite, or remove one exact paragraph"
    else:
        location = f"lines {issue['first_line']} and {issue['duplicate_line']}"
        fix = "Merge, rewrite, or remove one exact list item"
    return (
        f"Core hygiene conflict [{code}] at {location}. {fix}, "
        "then submit the complete revised document. No content was written."
    )


def _validate_budget(content: str) -> str:
    normalized = content.strip()
    tokens = estimate_tokens(normalized)
    if tokens > _max_tokens():
        raise CoreLimitError(
            f"Core is about {tokens} tokens; limit is {_max_tokens()}. "
            "Shorten it before saving."
        )
    return normalized


def _validate(content: str) -> str:
    normalized = _validate_budget(content)
    issues = _hygiene_issues(normalized)
    if issues:
        raise CoreHygieneError(_hygiene_error_message(issues[0]))
    return normalized


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_write(path: Path, content: str) -> None:
    _atomic_write_bytes(path, _serialize(content))


@contextmanager
def _interprocess_lock(timeout: float | None = None):
    """Hold a cross-platform advisory lock for one complete Core transaction."""
    timeout = CORE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    path = _core_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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
                    raise CoreLockTimeout(
                        f"Timed out after {timeout:.1f}s waiting for Core lock: {path}"
                    ) from exc
                time.sleep(CORE_LOCK_POLL_SECONDS)
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
    with _lock:
        with _interprocess_lock():
            yield


def _safe_source(source: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", source.strip()).strip("-")
    return cleaned or "unknown"


def _snapshot_unlocked(content: str, source: str) -> dict | None:
    if not content and not _core_path().exists():
        return None
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
    source_slug = _safe_source(source)
    digest = _sha256(content)[:12]
    snapshot_id = f"{timestamp}--{source_slug}--{digest}.md"
    path = _history_dir() / snapshot_id
    _atomic_write(path, content)
    snapshots = sorted(_history_dir().glob("*.md"), reverse=True)
    for stale in snapshots[SNAPSHOT_LIMIT:]:
        stale.unlink()
    return {
        "id": snapshot_id,
        "created_at": now.isoformat(),
        "source": source,
        "chars": len(content),
        "tokens": estimate_tokens(content),
    }


def read_core() -> str:
    with _transaction():
        _initialize_core_unlocked()
        return _core_path().read_text(encoding="utf-8").strip()


def replace_core(content: str, *, source: str = "admin") -> dict:
    with _transaction():
        _initialize_core_unlocked()
        normalized = _validate(content)
        current = _core_path().read_text(encoding="utf-8").strip()
        if current == normalized:
            return {"changed": False, **_stats(current)}
        _snapshot_unlocked(current, source)
        _atomic_write(_core_path(), normalized)
        return {"changed": True, **_stats(normalized)}


def replace_core_exact(
    *,
    expected_content: str,
    content: str,
    source: str = "main",
) -> dict:
    with _transaction():
        _initialize_core_unlocked()
        current = _core_path().read_text(encoding="utf-8").strip()
        if current != expected_content.strip():
            raise CoreConflictError(
                "Core changed since this turn began. Read the current document and "
                "submit a fresh revision."
            )
        updated = _validate(content)
        if updated == current:
            return {"changed": False, "content": current, **_stats(current)}
        _snapshot_unlocked(current, source)
        _atomic_write(_core_path(), updated)
        return {"changed": True, "content": updated, **_stats(updated)}


def _read_weekly_receipt_unlocked(user_id: int, period_key: str) -> dict:
    path = _weekly_receipt_path(user_id, period_key)
    if not path.is_file():
        return {}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return receipt if receipt.get("period_key") == period_key else {}


def has_weekly_core_update(user_id: int, period_key: str) -> bool:
    with _transaction():
        return bool(_read_weekly_receipt_unlocked(user_id, period_key))


def replace_weekly_core_exact(
    *,
    user_id: int,
    period_key: str,
    expected_content: str,
    content: str,
) -> str:
    """Commit a complete revision against the visible Core and weekly receipt."""
    if not isinstance(expected_content, str):
        raise CoreError("expected_content must be text.")
    updated = _validate(content)
    requested_hash = _sha256(updated)

    with _transaction():
        _initialize_core_unlocked()
        receipt = _read_weekly_receipt_unlocked(user_id, period_key)
        if receipt:
            return (
                "replayed"
                if receipt.get("content_sha256") == requested_hash
                else "conflict"
            )

        current = _core_path().read_text(encoding="utf-8").strip()
        if current != expected_content:
            # A process may have completed the durable Core write just before
            # persisting its receipt. Recover only an identical deterministic
            # target; all other concurrent edits remain conflicts.
            if current != updated:
                return "conflict"
            outcome = "replayed"
            snapshot = None
        elif current == updated:
            outcome = "committed"
            snapshot = None
        else:
            snapshot = _snapshot_unlocked(current, f"weekly-{period_key}")
            _atomic_write(_core_path(), updated)
            outcome = "committed"

        receipt = {
            "user_id": user_id,
            "period_key": period_key,
            "content_sha256": requested_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": snapshot,
        }
        _atomic_write(
            _weekly_receipt_path(user_id, period_key),
            json.dumps(receipt, ensure_ascii=False, indent=2),
        )
        return outcome


def _initialize_core_unlocked() -> None:
    if _core_path().is_file():
        return
    _atomic_write(_core_path(), _validate(DEFAULT_CORE))


def initialize_core() -> None:
    with _transaction():
        _initialize_core_unlocked()
