"""Note skill — add/list/remove notes in data/notes.md.

Notes are persistent working memory. Unlike diary (which records what happened),
notes track what needs attention going forward. The heartbeat Think loop reads
notes.md every patrol cycle, so any note written here will influence the bot's
proactive behavior.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from mochi.config import TZ
from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_NOTES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "notes.md"
_SECTION_HEADER = "## Notes"


def _read_notes() -> list[str]:
    """Read note lines from the ## Notes section."""
    if not _NOTES_PATH.exists():
        return []
    content = _NOTES_PATH.read_text(encoding="utf-8")
    in_section = False
    notes = []
    for line in content.splitlines():
        if line.strip() == _SECTION_HEADER:
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            stripped = line.strip()
            if stripped.startswith("- "):
                notes.append(stripped[2:])
    return notes


def _write_notes(notes: list[str]) -> None:
    """Rewrite the ## Notes section in notes.md, preserving other content."""
    if _NOTES_PATH.exists():
        content = _NOTES_PATH.read_text(encoding="utf-8")
    else:
        content = "# Notes\n"

    section = _SECTION_HEADER + "\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    pattern = re.compile(r"## Notes\n.*?(?=\n## |\Z)", re.DOTALL)
    if pattern.search(content):
        content = pattern.sub(section, content)
    else:
        content = content.rstrip() + "\n\n" + section

    _NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _NOTES_PATH.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(_NOTES_PATH)


# -- Public helpers for other modules --

def read_notes_for_observation(compact: bool = False) -> str:
    """Read data/notes.md for Think observation context.

    Returns formatted section string or "" if no notes.
    """
    if not _NOTES_PATH.exists():
        return ""
    content = _NOTES_PATH.read_text(encoding="utf-8").strip()
    if not content or content == "# Notes":
        return ""
    # Strip markdown headers — we add our own section header
    stripped_lines = []
    for line in content.split("\n"):
        if line.strip().startswith("#"):
            continue
        stripped_lines.append(line)
    body = "\n".join(stripped_lines).strip()
    if not body:
        return ""
    if compact:
        truncated = body[:300]
        if len(body) > 300:
            truncated += "\n..."
        return "## Notes\n" + truncated
    return "## Notes\n" + body


def archive_notes() -> dict:
    """Snapshot notes.md to monthly archive (append-only, no clearing).

    notes.md is persistent working memory — it should NOT be
    cleared nightly. We only take a daily snapshot for historical reference.
    """
    try:
        if not _NOTES_PATH.exists():
            return {"status": "ok", "archived": 0}

        content = _NOTES_PATH.read_text(encoding="utf-8").strip()
        # Only archive if there's real content beyond section headers
        lines = [l for l in content.split("\n")
                 if l.strip() and not l.startswith("#") and not l.startswith("(")]
        if not lines:
            return {"status": "ok", "archived": 0}

        # Snapshot to monthly archive file (append-only)
        now = datetime.now(TZ)
        archive_dir = _NOTES_PATH.parent / "notes_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{now.strftime('%Y-%m')}.md"

        date_str = now.strftime("%Y-%m-%d")
        with open(archive_file, "a", encoding="utf-8") as f:
            f.write(f"\n---\n## {date_str}\n\n{content}\n")

        # Do NOT clear notes.md — it's persistent working memory
        log.info("Notes snapshot saved to %s (content preserved)", archive_file.name)
        return {"status": "ok", "archived": 1}

    except Exception as e:
        log.error("Notes archive failed: %s", e, exc_info=True)
        return {"status": "error", "archived": 0, "error": str(e)}


class NoteSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = (args.get("action") or "").lower()

        if action == "add":
            return SkillResult(output=self._add(args))
        elif action == "list":
            return SkillResult(output=self._list())
        elif action == "remove":
            return SkillResult(output=self._remove(args))
        return SkillResult(
            output=f"Unknown action: {action}. Use add/list/remove.",
            success=False,
        )

    def _add(self, args: dict) -> str:
        content = (args.get("content") or "").strip()
        if not content:
            return "Error: content is required for add."

        today = datetime.now(TZ).strftime("%Y-%m-%d")
        # Append date tag if not already present
        if not re.search(r"\(\d{4}-\d{2}-\d{2}\)", content):
            content = f"{content} ({today})"

        notes = _read_notes()
        notes.append(content)
        _write_notes(notes)
        log.info("note add: '%s'", content[:80])
        return f"OK: note added ({len(notes)} total)."

    def _list(self) -> str:
        notes = _read_notes()
        if not notes:
            return "No notes."
        lines = [f"{i+1}. {n}" for i, n in enumerate(notes)]
        return "\n".join(lines)

    def _remove(self, args: dict) -> str:
        note_id = args.get("note_id")
        if note_id is None:
            return "Error: note_id is required for remove."

        try:
            idx = int(note_id) - 1
        except (ValueError, TypeError):
            return f"Error: note_id must be a number, got {note_id!r}."

        notes = _read_notes()
        if idx < 0 or idx >= len(notes):
            return f"Error: note_id {note_id} out of range (1-{len(notes)})."

        removed = notes.pop(idx)
        _write_notes(notes)
        log.info("note remove #%d: '%s'", int(note_id), removed[:80])
        return f"OK: removed note #{note_id} — \"{removed[:60]}\"."
