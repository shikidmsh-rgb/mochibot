"""Diary infrastructure — DailyFile class, shared diary instance, and status refresh.

Infrastructure layer (L4) for daily markdown files with append/dedup/archive.
Multiple modules write to the shared `diary` instance; this module owns the lock
and file I/O. No skill-layer logic here — just structured file operations.
"""

import logging
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

from mochi.config import (
    TZ,
    DIARY_STATUS_MAX_LINES,
    DIARY_ENTRY_MAX_LINES,
    OWNER_USER_ID,
)

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class DiaryArchiveWindow:
    content: str
    dates: tuple[str, ...]
    total_chars: int
    truncated: bool


class DiaryConflictError(ValueError):
    """The journal changed after Main received its visible snapshot."""


@dataclass(frozen=True)
class TomorrowDiaryDraft:
    source_date: str
    target_date: str
    content: str


def _atomic_replace_text(path: Path, content: str) -> None:
    """Replace a text file from a flushed temporary sibling."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _diary_date() -> datetime:
    """Effective date in TZ (rolls over at maintenance hour, not midnight)."""
    from mochi.admin.admin_db import get_system_config
    now = datetime.now(TZ)
    if now.hour < get_system_config("MAINTENANCE_HOUR"):
        now = now - timedelta(days=1)
    return now


def _today_str() -> str:
    return _diary_date().strftime("%Y-%m-%d")


def _now_time() -> str:
    return datetime.now(TZ).strftime("%H:%M")


def _format_line(entry: str, source: str) -> str:
    """Format an entry with timestamp and source prefix."""
    if source == "system":
        return f"- {entry}"
    prefix = "💭 " if source.startswith("think") else ""
    return f"- [{_now_time()}] {prefix}{entry}"


def _strip_to_core(line: str) -> str:
    """Extract core text from a line, stripping formatting."""
    core = line.lstrip("- ").strip()
    if core.startswith("[") and "]" in core:
        core = core.split("]", 1)[1].strip()
    if core.startswith("💭 "):
        core = core.removeprefix("💭 ").strip()
    return core


# ---------------------------------------------------------------------------
# DailyFile — daily markdown file with sections, dedup, and archive
# ---------------------------------------------------------------------------

class DailyFile:
    """A single daily markdown file with date-rolling header, dedup, and archive.

    Supports optional sections (e.g. "今日状態", "今日日記") for structured files.
    """

    def __init__(
        self,
        path: Path,
        label: str,
        max_lines: int,
        topic_dedup_prefixes: tuple[str, ...] = (),
        sections: tuple[str, ...] = (),
        section_max_lines: dict[str, int] | None = None,
    ):
        self.path = path
        self.label = label
        self.max_lines = max_lines
        self.topic_dedup_prefixes = topic_dedup_prefixes
        self.sections = sections
        self._section_max = section_max_lines or {}
        self._lock = Lock()

    # -- header helpers --

    def _header(self, date_str: str | None = None) -> str:
        d = (
            datetime.strptime(date_str, "%Y-%m-%d")
            if date_str is not None
            else _diary_date()
        )
        return f"# {self.label} {d.strftime('%Y-%m-%d')} {_WEEKDAYS[d.weekday()]}"

    def _ensure_today(self) -> str:
        """Ensure file exists with today's header (+ section headers). Returns content."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        today = _today_str()

        if self.path.exists():
            self._rollover_unlocked(today)
            content = self.path.read_text(encoding="utf-8").strip()
            if content:
                if self.sections and not any(
                    l.startswith("## ") for l in content.split("\n")
                ):
                    content = self._add_section_headers(content)
                    _atomic_replace_text(self.path, content + "\n")
                return self._reconcile_tomorrow_draft_unlocked(today, content)

        content = self._empty_document(today)
        _atomic_replace_text(self.path, content + "\n")
        return self._reconcile_tomorrow_draft_unlocked(today, content)

    def _empty_document(self, date_str: str) -> str:
        header = self._header(date_str)
        if self.sections:
            parts = [header]
            for sec in self.sections:
                parts.append(f"\n## {sec}")
            content = "\n".join(parts)
        else:
            content = header
        return content

    def _add_section_headers(self, content: str) -> str:
        parts = [content]
        for sec in self.sections:
            parts.append(f"\n## {sec}")
        return "\n".join(parts)

    def _parse_sections(self, content: str) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {"_header": []}
        current = "_header"
        for line in content.split("\n"):
            if line.startswith("## ") and line[3:].strip() in self.sections:
                current = line[3:].strip()
                result.setdefault(current, [])
            elif current == "_header" and line.startswith("# "):
                result["_header"].append(line)
            else:
                result.setdefault(current, [])
                if line.strip():
                    result[current].append(line)
        return result

    def _rebuild_from_sections(self, parsed: dict[str, list[str]]) -> str:
        parts = parsed.get("_header", [])
        for sec in self.sections:
            parts.append(f"\n## {sec}")
            for line in parsed.get(sec, []):
                parts.append(line)
        return "\n".join(parts)

    def _get_section_lines(self, content: str, section: str | None) -> list[str]:
        if not self.sections or section is None:
            lines = content.strip().split("\n")
            return [l for l in lines[1:] if l.strip() and not l.startswith("## ")]
        parsed = self._parse_sections(content)
        return parsed.get(section, [])

    def _max_for_section(self, section: str | None) -> int:
        if section and section in self._section_max:
            return self._section_max[section]
        return self.max_lines

    def _section_span(self, content: str, section: str) -> tuple[int, int]:
        heading = re.search(
            rf"(?m)^## {re.escape(section)}[ \t]*(?:\r?\n|\Z)", content,
        )
        if heading is None:
            raise ValueError(f"unknown section '{section}'")
        start = heading.end()
        reserved = "|".join(re.escape(item) for item in self.sections)
        following = re.search(
            rf"(?m)^## (?:{reserved})[ \t]*(?:\r?\n|\Z)", content[start:],
        )
        return start, start + following.start() if following else len(content)

    def _section_content(self, content: str, section: str) -> str:
        start, end = self._section_span(content, section)
        return content[start:end].strip()

    def _replace_section_content(self, content: str, section: str, body: str) -> str:
        start, end = self._section_span(content, section)
        before = content[:start].rstrip() + "\n"
        after = content[end:].lstrip("\r\n")
        normalized = body.strip()
        replacement = before + (normalized + "\n" if normalized else "")
        if after:
            replacement += "\n" + after
        return replacement.rstrip()

    # -- read --

    def read(self, section: str | None = None) -> str:
        """Read today's entries. section=None returns all entries (no header)."""
        with self._lock:
            content = self._ensure_today()
        if self.sections and section:
            return self._section_content(content, section)
        entries = self._get_section_lines(content, section)
        return "\n".join(entries)

    def read_raw(self) -> str:
        """Read raw file content including header. For archive use."""
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8").strip()

    # -- write --

    def append(self, entry: str, source: str = "chat", section: str | None = None) -> str:
        """Append an entry with timestamp. Deduplicates by topic prefix and exact match."""
        entry = entry.strip()
        if not entry:
            return "Error: entry is empty."

        line = _format_line(entry, source)

        with self._lock:
            content = self._ensure_today()
            entry_lines = self._get_section_lines(content, section)
            max_lines = self._max_for_section(section)

            core_text = _strip_to_core(line)
            topic_prefix = None
            for pfx in self.topic_dedup_prefixes:
                if core_text.startswith(pfx):
                    topic_prefix = pfx
                    break
            for existing in entry_lines:
                existing_core = _strip_to_core(existing)
                if topic_prefix and existing_core.startswith(topic_prefix):
                    log.debug("%s dedup: topic '%s' already present", self.label, topic_prefix)
                    return f"Already recorded: {entry}"
                if existing_core == core_text:
                    log.debug("%s dedup: skipping duplicate '%s'", self.label, entry)
                    return f"Already recorded: {entry}"

            if len(entry_lines) >= max_lines:
                entry_lines = entry_lines[-(max_lines - 5):]
                log.warning("%s reached %d lines, trimmed", self.label, max_lines)

            entry_lines.append(line)
            self._write_section(content, section, entry_lines)

        log.info("%s entry added: %s", self.label, line)
        return f"Recorded: {entry}"

    def upsert(self, key: str, entry: str, source: str = "system",
               section: str | None = None) -> str:
        """Insert or replace an entry by key prefix."""
        entry = entry.strip()
        if not entry:
            return "Error: entry is empty."

        new_line = _format_line(entry, source)

        with self._lock:
            content = self._ensure_today()
            entry_lines = self._get_section_lines(content, section)
            max_lines = self._max_for_section(section)

            replaced = False
            for i, existing in enumerate(entry_lines):
                if _strip_to_core(existing).startswith(key):
                    entry_lines[i] = new_line
                    replaced = True
                    break

            if not replaced:
                if len(entry_lines) >= max_lines:
                    entry_lines = entry_lines[-(max_lines - 5):]
                entry_lines.append(new_line)

            self._write_section(content, section, entry_lines)

        action = "replaced" if replaced else "added"
        log.info("%s entry %s (key=%s): %s", self.label, action, key, new_line)
        return f"{'Replaced' if replaced else 'Recorded'}: {entry}"

    def remove(self, key: str, section: str | None = None) -> str:
        """Remove entry matching key prefix. Idempotent."""
        with self._lock:
            content = self._ensure_today()
            entry_lines = self._get_section_lines(content, section)

            filtered = [l for l in entry_lines if not _strip_to_core(l).startswith(key)]
            if len(filtered) == len(entry_lines):
                return f"Not found: {key}"

            removed_count = len(entry_lines) - len(filtered)
            self._write_section(content, section, filtered)

        log.info("%s entry removed (key=%s), %d line(s)", self.label, key, removed_count)
        return f"Removed {removed_count} entry(s) matching: {key}"

    def rewrite(self, entries: str, section: str | None = None) -> str:
        """Overwrite today's file (or section) with new entries. Header is auto-managed."""
        entries = entries.strip()
        if not entries:
            return "Error: entries cannot be empty."

        entry_lines = [l for l in entries.split("\n") if l.strip()]
        max_lines = self._max_for_section(section)
        if len(entry_lines) > max_lines:
            entry_lines = entry_lines[:max_lines]
            log.warning("%s rewrite: trimmed to %d lines", self.label, max_lines)

        with self._lock:
            if self.sections and section:
                content = self._ensure_today()
                self._write_section(content, section, entry_lines)
            else:
                header = self._header()
                content = header + "\n" + "\n".join(entry_lines)
                self.path.write_text(content + "\n", encoding="utf-8")

        log.info("%s rewritten with %d entries", self.label, len(entry_lines))
        return f"{self.label} rewritten with {len(entry_lines)} entries."

    def rewrite_section(self, section: str, lines: list[str]) -> str:
        """Replace entire section content."""
        if not self.sections:
            return "Error: file has no sections."
        if section not in self.sections:
            return f"Error: unknown section '{section}'."

        max_lines = self._max_for_section(section)
        if len(lines) > max_lines:
            lines = lines[:max_lines]

        with self._lock:
            content = self._ensure_today()
            self._write_section(content, section, lines)

        return f"{self.label} section '{section}' rewritten with {len(lines)} entries."

    def _write_section(self, content: str, section: str | None,
                       entry_lines: list[str]) -> None:
        """Write entry_lines to a section. Must be called under lock."""
        if not self.sections or section is None:
            header_line = content.strip().split("\n")[0]
            all_lines = [header_line] + entry_lines
            _atomic_replace_text(self.path, "\n".join(all_lines) + "\n")
        else:
            rebuilt = self._replace_section_content(
                content, section, "\n".join(entry_lines),
            )
            _atomic_replace_text(self.path, rebuilt + "\n")

    def replace_section_exact(
        self, section: str, *, expected_content: str, content: str,
        target_date: str,
    ) -> dict:
        with self._lock:
            document = self._ensure_today()
            if self._header_date(document) != target_date:
                raise DiaryConflictError(
                    "The logical day changed since this turn began."
                )
            current = self._section_content(document, section)
            if current != expected_content.strip():
                raise DiaryConflictError(
                    "Today's journal changed since this turn began."
                )
            normalized = content.strip()
            changed = normalized != current
            if changed:
                updated = self._replace_section_content(document, section, normalized)
                _atomic_replace_text(self.path, updated + "\n")
            return {"changed": changed, "chars": len(normalized), "content": normalized}

    @property
    def tomorrow_draft_path(self) -> Path:
        return self.path.parent / "diary_tomorrow.json"

    def _read_tomorrow_draft_unlocked(self) -> TomorrowDiaryDraft | None:
        if not self.tomorrow_draft_path.exists():
            return None
        try:
            payload = json.loads(self.tomorrow_draft_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Tomorrow Diary draft is malformed") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "source_date", "target_date", "content",
        }:
            raise ValueError("Tomorrow Diary draft has invalid fields")
        if not all(isinstance(value, str) for value in payload.values()):
            raise ValueError("Tomorrow Diary draft has invalid values")
        source = date.fromisoformat(payload["source_date"])
        target = date.fromisoformat(payload["target_date"])
        if target != source + timedelta(days=1):
            raise ValueError("Tomorrow Diary target must follow its source date")
        return TomorrowDiaryDraft(
            source_date=payload["source_date"],
            target_date=payload["target_date"],
            content=payload["content"].strip(),
        )

    def read_write_snapshot(self) -> tuple[str, str, str]:
        with self._lock:
            document = self._ensure_today()
            source = self._header_date(document)
            target = (date.fromisoformat(source) + timedelta(days=1)).isoformat()
            draft = self._read_tomorrow_draft_unlocked()
            tomorrow = draft.content if draft and draft.target_date == target else ""
            return source, self._section_content(document, "今日日記"), tomorrow

    def read_tomorrow_draft(self, target_date: str) -> str:
        with self._lock:
            draft = self._read_tomorrow_draft_unlocked()
            return draft.content if draft and draft.target_date == target_date else ""

    def replace_tomorrow_exact(
        self, *, source_date: str, target_date: str,
        expected_content: str, content: str,
    ) -> dict:
        source = date.fromisoformat(source_date)
        target = date.fromisoformat(target_date)
        if target != source + timedelta(days=1):
            raise ValueError("Tomorrow Diary target must be the next logical day")
        with self._lock:
            document = self._ensure_today()
            if self._header_date(document) != source_date:
                raise DiaryConflictError(
                    "The logical day changed since this turn began."
                )
            draft = self._read_tomorrow_draft_unlocked()
            if draft is not None and draft.target_date != target_date:
                raise DiaryConflictError("Another Tomorrow Diary target is already staged.")
            current = draft.content if draft else ""
            if current != expected_content.strip():
                raise DiaryConflictError(
                    "Tomorrow's journal changed since this turn began."
                )
            normalized = content.strip()
            changed = current != normalized
            if changed:
                if normalized:
                    _atomic_replace_text(self.tomorrow_draft_path, json.dumps({
                        "source_date": source_date,
                        "target_date": target_date,
                        "content": normalized,
                    }, ensure_ascii=False) + "\n")
                else:
                    self.tomorrow_draft_path.unlink(missing_ok=True)
            return {"changed": changed, "chars": len(normalized), "content": normalized}

    def _archive_missed_draft_unlocked(self, draft: TomorrowDiaryDraft) -> None:
        archive_path = (
            self.path.parent / f"{self.label.lower()}_archive"
            / f"{draft.target_date[:7]}.md"
        )
        archived = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        existing = dict(self._archive_blocks(archived)).get(draft.target_date)
        if existing is None:
            document = self._replace_section_content(
                self._empty_document(draft.target_date), "今日日記", draft.content,
            )
            _atomic_replace_text(archive_path, f"{archived.rstrip()}\n\n{document}\n\n".lstrip())
            return
        current = self._section_content(existing, "今日日記")
        if draft.content not in current:
            updated = self._replace_section_content(
                existing, "今日日記",
                "\n\n".join(part for part in (current, draft.content) if part),
            )
            _atomic_replace_text(archive_path, archived.replace(existing, updated, 1))

    def _reconcile_tomorrow_draft_unlocked(self, today: str, document: str) -> str:
        draft = self._read_tomorrow_draft_unlocked()
        if draft is None or draft.target_date > today:
            return document
        if draft.target_date < today:
            self._archive_missed_draft_unlocked(draft)
        else:
            current = self._section_content(document, "今日日記")
            if draft.content not in current:
                document = self._replace_section_content(
                    document, "今日日記",
                    "\n\n".join(part for part in (draft.content, current) if part),
                )
                _atomic_replace_text(self.path, document + "\n")
        self.tomorrow_draft_path.unlink(missing_ok=True)
        return document

    # -- archive --

    def _header_date(self, raw: str) -> str:
        first_line = raw.splitlines()[0] if raw.splitlines() else ""
        match = re.fullmatch(
            rf"# {re.escape(self.label)} (\d{{4}}-\d{{2}}-\d{{2}})(?:\s+.*)?",
            first_line,
        )
        if not match:
            raise ValueError(f"{self.label} header is malformed")
        date_str = match.group(1)
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str

    def _has_entries(self, raw: str) -> bool:
        return any(
            line.strip() and not line.startswith("#")
            for line in raw.splitlines()[1:]
        )

    def _archive_blocks(self, raw: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            rf"(?m)^# {re.escape(self.label)} "
            r"(\d{4}-\d{2}-\d{2})(?:\s+.*)?$"
        )
        matches = list(pattern.finditer(raw))
        return [
            (
                match.group(1),
                raw[
                    match.start():
                    matches[index + 1].start() if index + 1 < len(matches) else len(raw)
                ].strip(),
            )
            for index, match in enumerate(matches)
        ]

    def _rollover_unlocked(self, current_logical_day: str) -> str:
        if not self.path.exists():
            return "missing"
        raw = self.path.read_text(encoding="utf-8")
        if not raw.strip():
            return "empty"
        source_day = self._header_date(raw)
        if source_day > current_logical_day:
            raise ValueError(
                f"{self.label} header date {source_day} is after {current_logical_day}"
            )
        if source_day == current_logical_day:
            return "current"
        block = raw.strip()
        if not self._has_entries(block):
            _atomic_replace_text(self.path, "")
            return "empty_stale"

        archive_dir = self.path.parent / f"{self.label.lower()}_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"{source_day[:7]}.md"
        archived = (
            archive_path.read_text(encoding="utf-8")
            if archive_path.exists()
            else ""
        )
        existing = [
            existing_block
            for date_str, existing_block in self._archive_blocks(archived)
            if date_str == source_day
        ]
        if existing:
            if len(existing) != 1 or existing[0] != block:
                raise ValueError(
                    f"{self.label} archive already has conflicting content for {source_day}"
                )
            result = "already_archived"
        else:
            combined = (
                f"{archived.rstrip()}\n\n{block}\n\n"
                if archived.strip()
                else f"{block}\n\n"
            )
            _atomic_replace_text(archive_path, combined)
            result = "archived"
        _atomic_replace_text(self.path, "")
        return result

    def rollover(self, current_logical_day: str | None = None) -> str:
        """Archive stale content and clear it only after archive success."""
        with self._lock:
            return self._rollover_unlocked(current_logical_day or _today_str())


# ---------------------------------------------------------------------------
# Module-level diary instance
# ---------------------------------------------------------------------------

diary = DailyFile(
    path=_DATA_DIR / "diary.md",
    label="Diary",
    max_lines=DIARY_STATUS_MAX_LINES,
    sections=("今日状態", "今日日記"),
    section_max_lines={
        "今日状態": DIARY_STATUS_MAX_LINES,
        "今日日記": DIARY_ENTRY_MAX_LINES,
    },
)


def read_diary_archive_window(
    start: date,
    end: date,
    *,
    max_chars: int = 12_000,
) -> DiaryArchiveWindow:
    """Read archived Diary blocks in [start, end) without causing rollover."""
    if not isinstance(start, date) or not isinstance(end, date) or start >= end:
        raise ValueError("Diary archive window must be an increasing date range")
    if max_chars < 100:
        raise ValueError("Diary archive budget is too small")

    archive_dir = diary.path.parent / "diary_archive"
    monthly_blocks: dict[str, dict[str, str]] = {}
    selected: list[str] = []
    dates: list[str] = []
    current = start
    while current < end:
        date_str = current.isoformat()
        month = date_str[:7]
        if month not in monthly_blocks:
            archive_path = archive_dir / f"{month}.md"
            raw = (
                archive_path.read_text(encoding="utf-8")
                if archive_path.exists()
                else ""
            )
            monthly_blocks[month] = {
                block_date: block
                for block_date, block in diary._archive_blocks(raw)
            }
        block = monthly_blocks[month].get(date_str)
        if block:
            selected.append(block)
            dates.append(date_str)
        current += timedelta(days=1)

    raw_content = "\n\n".join(selected)
    total_chars = len(raw_content)
    truncated = total_chars > max_chars
    marker = "\n\n[Diary archive truncated by system budget]"
    content = (
        raw_content[:max_chars - len(marker)].rstrip() + marker
        if truncated
        else raw_content
    )
    return DiaryArchiveWindow(
        content=content,
        dates=tuple(dates),
        total_chars=total_chars,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Status refresh — rebuild 今日状態 from DB
# ---------------------------------------------------------------------------

def refresh_diary_status(user_id: int | None = None) -> str:
    """Rebuild the 今日状態 section of diary.md from current DB state.

    Delegates to each skill's diary_status() method via the skill registry.
    Called by heartbeat tick and after habit checkins.
    """
    if user_id is None:
        user_id = OWNER_USER_ID
    if user_id is None:
        return "No user configured."

    now = datetime.now(TZ)
    from mochi.config import logical_today
    today = logical_today(now)

    try:
        from mochi.skills import collect_diary_status
        lines = collect_diary_status(user_id, today, now)
    except Exception:
        log.exception("diary_status: collect_diary_status failed")
        lines = []

    if lines:
        return diary.rewrite_section("今日状態", lines)

    from mochi.skills import _skills
    if not _skills:
        log.warning("diary_status: skill registry empty — skipping overwrite")
        return diary.read()

    return diary.rewrite_section("今日状態", ["- (nothing tracked today)"])
