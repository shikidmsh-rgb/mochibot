"""Diary skill — daily working memory with auto-archive.

File-based storage:
  - Working:  data/diary.md  (today's entries)
  - Archive:  data/diary_archive/YYYY-MM.md  (monthly rollups)

Entries roll over at MAINTENANCE_HOUR. The nightly pipeline archives
yesterday's diary and clears the working file.
"""

import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.config import TIMEZONE_OFFSET_HOURS, MAINTENANCE_HOUR

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data"
_DIARY_PATH = _DATA_DIR / "diary.md"
_ARCHIVE_DIR = _DATA_DIR / "diary_archive"
_file_lock = Lock()  # Protect concurrent reads/writes to diary.md


def _lazy_load_limits() -> tuple[int, int]:
    """Load configurable limits (avoid circular import at module level)."""
    from mochi.config import DIARY_MAX_LINES, DIARY_TRIM_TO
    return DIARY_MAX_LINES, DIARY_TRIM_TO


def _diary_date() -> datetime:
    """Logical diary date. Before MAINTENANCE_HOUR, entries belong to yesterday."""
    now = datetime.now(TZ)
    if now.hour < MAINTENANCE_HOUR:
        return now - timedelta(days=1)
    return now


def _ensure_header(content: str, date: datetime) -> str:
    """Ensure diary starts with date header."""
    header = f"# Diary {date.strftime('%Y-%m-%d %A')}"
    if content.startswith("# Diary"):
        # Replace existing header
        lines = content.split("\n")
        lines[0] = header
        return "\n".join(lines)
    return f"{header}\n\n{content}" if content else header


def read_diary(query: str = "") -> str:
    """Read today's diary entries. Optionally filter by keyword."""
    with _file_lock:
        if not _DIARY_PATH.exists():
            return "(Diary is empty today)"

        content = _DIARY_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return "(Diary is empty today)"

        # Skip header line for content
        lines = content.split("\n")
        entries = [l for l in lines[1:] if l.strip()]

    if not entries:
        return "(Diary is empty today)"

    if query:
        query_lower = query.lower()
        entries = [l for l in entries if query_lower in l.lower()]
        if not entries:
            return f"(No diary entries matching '{query}')"

    return "\n".join(entries)


def append_entry(content: str, source: str = "chat") -> str:
    """Append an entry to today's diary. Deduplicates by core text."""
    date = _diary_date()
    max_lines, trim_to = _lazy_load_limits()

    with _file_lock:
        # Read existing
        existing = ""
        if _DIARY_PATH.exists():
            existing = _DIARY_PATH.read_text(encoding="utf-8").strip()

        # Check for dedup — strip timestamps and markers for comparison
        core_text = re.sub(r"^\[[\d:]+\]\s*", "", content.strip())
        core_text = re.sub(r"^[💭🔧]\s*", "", core_text)
        if existing:
            for line in existing.split("\n"):
                line_core = re.sub(r"^\[[\d:]+\]\s*", "", line.strip())
                line_core = re.sub(r"^[💭🔧-]\s*", "", line_core)
                if line_core and core_text and line_core.strip() == core_text.strip():
                    return "Entry already exists (deduplicated)."

        # Format entry
        now = datetime.now(TZ)
        timestamp = now.strftime("%H:%M")
        prefix = "💭 " if source == "think" else ""
        entry = f"- [{timestamp}] {prefix}{content.strip()}"

        # Append
        if existing:
            new_content = f"{existing}\n{entry}"
        else:
            new_content = _ensure_header("", date) + f"\n\n{entry}"

        # Trim if over limit
        lines = new_content.split("\n")
        non_empty = [l for l in lines if l.strip() and not l.startswith("#")]
        if len(non_empty) > max_lines:
            # Keep header + last trim_to entries
            header = lines[0] if lines[0].startswith("#") else ""
            entries = [l for l in lines if l.strip() and not l.startswith("#")]
            trimmed = entries[-trim_to:]
            new_content = header + "\n\n" + "\n".join(trimmed) if header else "\n".join(trimmed)

        _DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DIARY_PATH.write_text(new_content + "\n", encoding="utf-8")

    return "Entry added."


def rewrite_diary(content: str) -> str:
    """Replace all diary entries with new content."""
    date = _diary_date()
    new_content = _ensure_header(content.strip(), date)
    with _file_lock:
        _DIARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DIARY_PATH.write_text(new_content + "\n", encoding="utf-8")
    return "Diary rewritten."


def save_diary_snapshot() -> str:
    """Archive current diary to monthly file. Called by nightly maintenance."""
    with _file_lock:
        if not _DIARY_PATH.exists():
            return "Nothing to archive."

        content = _DIARY_PATH.read_text(encoding="utf-8").strip()
        if not content:
            return "Nothing to archive."

        date = _diary_date()
        archive_file = _ARCHIVE_DIR / f"{date.strftime('%Y-%m')}.md"
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        # Append to monthly archive
        with open(archive_file, "a", encoding="utf-8") as f:
            f.write(f"\n\n{content}\n")

        # Clear working diary
        _DIARY_PATH.write_text("", encoding="utf-8")

    log.info("Diary archived to %s", archive_file.name)
    return f"Archived to {archive_file.name} and cleared."


class DiarySkill(Skill):
    """Daily working memory skill."""

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args

        if tool == "read_diary":
            output = read_diary(args.get("query", ""))
            return SkillResult(output=output)

        elif tool == "update_diary":
            action = args.get("action", "append")
            content = args.get("content", "")
            if not content:
                return SkillResult(output="No content provided.", success=False)

            if action == "rewrite":
                output = rewrite_diary(content)
            else:
                output = append_entry(content)
            return SkillResult(output=output)

        return SkillResult(output=f"Unknown diary tool: {tool}", success=False)
