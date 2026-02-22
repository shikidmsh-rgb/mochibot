"""Prompt loader — hot-reload prompt templates from prompts/ directory.

Edit prompt files directly — changes take effect immediately.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_cache: dict[str, str] = {}


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
