"""Diary infrastructure — DailyFile class, shared diary instance, and status refresh.

Infrastructure layer (L4) for daily markdown files with append/dedup/archive.
Multiple modules write to the shared `diary` instance; this module owns the lock
and file I/O. No skill-layer logic here — just structured file operations.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from mochi.config import (
    TZ,
    DIARY_STATUS_MAX_LINES,
    DIARY_ENTRY_MAX_LINES,
    MAINTENANCE_HOUR,
    OWNER_USER_ID,
)

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _diary_date() -> datetime:
    """Effective date in TZ (rolls over at maintenance hour, not midnight)."""
    now = datetime.now(TZ)
    if now.hour < MAINTENANCE_HOUR:
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

    def _header(self) -> str:
        d = _diary_date()
        return f"# {self.label} {d.strftime('%Y-%m-%d')} {_WEEKDAYS[d.weekday()]}"

    def _ensure_today(self) -> str:
        """Ensure file exists with today's header (+ section headers). Returns content."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            content = self.path.read_text(encoding="utf-8").strip()
            today = _today_str()
            if content and today in content.split("\n")[0]:
                if self.sections and not any(
                    l.startswith("## ") for l in content.split("\n")
                ):
                    content = self._add_section_headers(content)
                    self.path.write_text(content + "\n", encoding="utf-8")
                return content
            log.info("%s date mismatch, starting fresh for %s", self.label, today)

        header = self._header()
        if self.sections:
            parts = [header]
            for sec in self.sections:
                parts.append(f"\n## {sec}")
            content = "\n".join(parts)
        else:
            content = header
        self.path.write_text(content + "\n", encoding="utf-8")
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

    # -- read --

    def read(self, section: str | None = None) -> str:
        """Read today's entries. section=None returns all entries (no header)."""
        with self._lock:
            content = self._ensure_today()
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
        if len(entry) > 100:
            entry = entry[:97] + "..."

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
        if len(entry) > 100:
            entry = entry[:97] + "..."

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
            self.path.write_text("\n".join(all_lines) + "\n", encoding="utf-8")
        else:
            parsed = self._parse_sections(content)
            parsed[section] = entry_lines
            rebuilt = self._rebuild_from_sections(parsed)
            self.path.write_text(rebuilt + "\n", encoding="utf-8")

    # -- archive --

    def snapshot(self, raw: str) -> None:
        """Append raw content to monthly archive file."""
        if not raw or not raw.strip():
            return
        archive_dir = self.path.parent / f"{self.label.lower()}_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        date_part = ""
        for part in raw.split("\n")[0].split():
            if len(part) == 10 and part.count("-") == 2:
                date_part = part[:7]
                break
        if not date_part:
            date_part = datetime.now(TZ).strftime("%Y-%m")
        archive_path = archive_dir / f"{date_part}.md"
        with self._lock:
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(raw.strip() + "\n\n")
        log.info("%s snapshot saved to %s", self.label, archive_path.name)

    def clear(self) -> None:
        """Clear file (used after archiving)."""
        with self._lock:
            self.path.write_text("", encoding="utf-8")
        log.info("%s cleared after archive", self.label)


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


# ---------------------------------------------------------------------------
# Status refresh — rebuild 今日状態 from DB
# ---------------------------------------------------------------------------

def refresh_diary_status(user_id: int | None = None) -> str:
    """Rebuild the 今日状態 section of diary.md from current DB state.

    Queries habits, todos, and reminders to produce a structured snapshot.
    Called by heartbeat tick and after habit checkins.
    """
    if user_id is None:
        user_id = OWNER_USER_ID
    if not user_id:
        return "No user configured."

    now = datetime.now(TZ)
    from mochi.config import logical_today
    today = logical_today(now)
    lines: list[str] = []

    # -- Habits --
    try:
        from mochi.db import list_habits, get_habit_checkins, get_latest_habit_checkins_for_period
        from mochi.skills.habit.logic import parse_frequency, get_allowed_days

        habits = list_habits(user_id, active_only=True)
        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()

        for h in habits:
            # Skip paused
            paused_until = h.get("paused_until")
            if paused_until and paused_until >= today:
                continue

            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week

            checkins = get_habit_checkins(h["id"], period)
            done = len(checkins)

            # Skip weekly_on habits on non-active days (unless already done)
            allowed = get_allowed_days(h["frequency"])
            if allowed is not None and weekday not in allowed and done < target:
                continue

            name = h["name"]
            imp = "⚡" if h.get("importance") == "important" else ""
            ctx = h.get("context", "")
            ctx_tag = f" ({ctx})" if ctx else ""

            # Last checkin time for partially done habits
            last_tag = ""
            if 0 < done < target and checkins:
                last_at = checkins[-1].get("logged_at")
                if last_at:
                    try:
                        t = datetime.fromisoformat(last_at)
                        last_tag = f" last:{t.strftime('%H:%M')}"
                    except (ValueError, TypeError):
                        pass

            if done >= target:
                lines.append(f"- {imp}{name} ({done}/{target}) ✅")
            else:
                lines.append(f"- {imp}{name} ({done}/{target}){ctx_tag}{last_tag} ⏳")
    except Exception:
        log.exception("diary_status: failed to query habits")

    # -- Todos --
    try:
        from mochi.skills.todo.queries import get_visible_todos
        todos = get_visible_todos(today)
        for t in todos:
            overdue = t.get("nudge_date") and t["nudge_date"] < today
            tag = " ⚠️逾期" if overdue else ""
            lines.append(f"- [ ] {t['task']} [todo_id={t['id']}]{tag}")
    except Exception:
        log.exception("diary_status: failed to query todos")

    # -- Reminders --
    try:
        from mochi.db import get_pending_reminders
        from datetime import date as date_type

        pending_reminders = get_pending_reminders()
        logical_date = date_type.fromisoformat(today)
        for r in pending_reminders:
            remind_at_str = r["remind_at"]
            try:
                remind_at = datetime.fromisoformat(remind_at_str)
                if remind_at.date() == logical_date:
                    time_str = remind_at.strftime("%H:%M")
                    fired = remind_at <= now
                    mark = "✅" if fired else "⏳"
                    lines.append(f"- {time_str} {r['message']} {mark}")
            except (ValueError, TypeError):
                pass
    except Exception:
        log.exception("diary_status: failed to query reminders")

    # -- Write to diary --
    if lines:
        return diary.rewrite_section("今日状態", lines)
    return diary.rewrite_section("今日状態", ["- (nothing tracked today)"])
