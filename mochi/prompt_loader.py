"""Prompt loader — hot-reload prompt templates from prompts/ directory.

Edit prompt files directly — changes take effect immediately.
"""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
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


# ── Modular system_chat prompt assembly ──────────────────────────────

# Stable identity lives in Core, outside the bundled runtime templates.
_SYSTEM_CHAT_MODULE_ORDER = ("agent", "runtime_context")


def _is_empty_template(content: str) -> bool:
    """True if content is only heading lines (no real body text)."""
    return all(
        line.startswith("#") or not line
        for line in content.strip().splitlines()
    )


def get_system_chat_modules() -> dict[str, str]:
    """Load system_chat/*.md in fixed order, returning name→content mapping.

    Core is stored separately in data/core.md and is never loaded here.
    """
    modules: dict[str, str] = {}
    for name in _SYSTEM_CHAT_MODULE_ORDER:
        key = f"system_chat/{name}"
        content = get_prompt(key)
        if not content or _is_empty_template(content):
            continue
        modules[name] = content
    return modules
