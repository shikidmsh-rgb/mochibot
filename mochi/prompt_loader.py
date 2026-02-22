"""Prompt loader — hot-reload prompt templates from prompts/ directory.

Supports personality.md with ## sections (Chat, Think) that get
auto-prepended to task prompts. Edit prompt files directly —
changes take effect immediately.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_cache: dict[str, str] = {}

# Prompts that are pure functional — never prepend personality
_NO_PERSONALITY = {"memory_extract", "personality"}


def _extract_section(text: str, heading: str) -> str:
    """Extract content under a specific ## heading from markdown.

    Returns everything between '## <heading>' and the next '## ' or EOF.
    """
    pattern = rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def get_personality(section: str = "Chat") -> str:
    """Load a specific section from personality.md.

    Sections: 'Chat' (for conversations/reports), 'Think' (for heartbeat).
    Returns empty string if personality.md or section not found.
    """
    full = get_prompt("personality")
    if not full:
        return ""
    return _extract_section(full, section)


def get_prompt(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    Always reads from disk (hot-reload). Falls back to cache if file missing.
    """
    path = _PROMPTS_DIR / f"{name}.md"
    if path.exists():
        content = path.read_text(encoding="utf-8").strip()
        _cache[name] = content
        return content

    if name in _cache:
        log.warning("Prompt file missing, using cache: %s", name)
        return _cache[name]

    log.error("Prompt not found: %s", name)
    return ""


def get_full_prompt(name: str, section: str = "Chat") -> str:
    """Load a prompt with personality prepended.

    personality.md[section] + '---' + task prompt.
    Functional prompts (memory_extract) skip personality.
    """
    task = get_prompt(name)
    if name in _NO_PERSONALITY:
        return task

    personality = get_personality(section)
    if not personality:
        return task

    return f"{personality}\n\n---\n\n{task}"


def reload_all() -> dict[str, int]:
    """Reload all prompts from disk. Returns {name: char_count}."""
    result = {}
    if not _PROMPTS_DIR.exists():
        return result
    for f in _PROMPTS_DIR.glob("*.md"):
        name = f.stem
        content = f.read_text(encoding="utf-8").strip()
        _cache[name] = content
        result[name] = len(content)
    log.info("Reloaded %d prompts", len(result))
    return result


def list_prompts() -> list[str]:
    """List available prompt template names."""
    if not _PROMPTS_DIR.exists():
        return []
    return sorted(f.stem for f in _PROMPTS_DIR.glob("*.md"))
