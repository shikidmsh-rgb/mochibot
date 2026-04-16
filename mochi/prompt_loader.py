"""Prompt loader — hot-reload prompt templates from prompts/ directory.

Edit prompt files directly — changes take effect immediately.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_DATA_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "prompts"
_cache: dict[str, str] = {}

# Prompts that users may override via data/prompts/ (survives git pull)
_USER_OVERRIDABLE = {"system_chat/soul", "system_chat/user"}


def get_prompt(name: str) -> str:
    """Load a prompt template by name (without .md extension).

    For user-overridable prompts (soul, user), checks data/prompts/ first.
    Always reads from disk (hot-reload). Falls back to cache if file missing.
    """
    # Check user override first
    if name in _USER_OVERRIDABLE:
        override = _DATA_PROMPTS_DIR / f"{name}.md"
        if override.exists():
            content = override.read_text(encoding="utf-8").strip()
            _cache[name] = content
            return content

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


# ── Modular system_chat prompt assembly ──────────────────────────────

_SYSTEM_CHAT_DIR = _PROMPTS_DIR / "system_chat"
_SYSTEM_CHAT_MODULE_ORDER = ("soul", "user", "agent", "runtime_context")


def _is_empty_template(content: str) -> bool:
    """True if content is only heading lines (no real body text)."""
    return all(
        line.startswith("#") or not line
        for line in content.strip().splitlines()
    )


def get_system_chat_modules() -> dict[str, str]:
    """Load system_chat/*.md in fixed order, returning name→content mapping.

    Skips modules whose files are empty templates (headings only, e.g.
    unfilled user.md).  For user-overridable modules, checks data/prompts/
    first.
    """
    modules: dict[str, str] = {}
    for name in _SYSTEM_CHAT_MODULE_ORDER:
        key = f"system_chat/{name}"
        content = get_prompt(key)
        if not content or _is_empty_template(content):
            continue
        modules[name] = content
    return modules


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

    # Also include subdirectory files
    for subdir in _PROMPTS_DIR.iterdir():
        if subdir.is_dir():
            for f in subdir.glob("*.md"):
                name = f"{subdir.name}/{f.stem}"
                content = f.read_text(encoding="utf-8").strip()
                _cache[name] = content
                result[name] = len(content)

    log.info("Reloaded %d prompts", len(result))
    return result


def list_prompts() -> list[str]:
    """List available prompt template names."""
    if not _PROMPTS_DIR.exists():
        return []
    names = sorted(f.stem for f in _PROMPTS_DIR.glob("*.md"))

    # Include subdirectory files
    for subdir in sorted(_PROMPTS_DIR.iterdir()):
        if subdir.is_dir():
            names.extend(sorted(f"{subdir.name}/{f.stem}" for f in subdir.glob("*.md")))

    return names
