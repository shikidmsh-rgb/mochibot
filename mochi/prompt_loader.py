"""Prompt loader — hot-reload prompt templates from prompts/ directory.

Supports two modes:
  - Legacy (default): personality.md + task prompts (single-file)
  - Modular (SYSTEM_PROMPT_MODULAR_ENABLED=true): assembly from
    prompts/system_chat/ directory with fixed module order

Edit prompt files directly — changes take effect immediately.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_cache: dict[str, str] = {}

# Prompts that are pure functional — never prepend personality
_NO_PERSONALITY = {"memory_extract", "personality"}

# Modular assembly: fixed order, immutable
_SYSTEM_CHAT_MODULE_ORDER = ("soul", "user", "tools", "runtime_context")


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


# ═══════════════════════════════════════════════════════════════════════════
# Modular Prompt Assembly
# ═══════════════════════════════════════════════════════════════════════════

def get_system_chat_modules(modular: bool | None = None) -> tuple[str, dict[str, str], str | None]:
    """Assemble system_chat prompt from modular fragments.

    Args:
        modular: Force modular mode. None = read from config.

    Returns:
        (assembled_prompt, {module_name: content}, fallback_reason or None)
          - fallback_reason is set when modular was requested but failed
    """
    if modular is None:
        from mochi.config import SYSTEM_PROMPT_MODULAR_ENABLED
        modular = SYSTEM_PROMPT_MODULAR_ENABLED

    if not modular:
        # Legacy mode: single file
        content = get_full_prompt("system_chat", "Chat")
        return content, {"system_chat": content}, None

    # Modular mode: load from prompts/system_chat/*.md
    module_dir = _PROMPTS_DIR / "system_chat"
    if not module_dir.exists():
        log.warning("Modular prompt dir not found: %s — falling back to legacy", module_dir)
        content = get_full_prompt("system_chat", "Chat")
        return content, {"system_chat": content}, "directory not found"

    modules: dict[str, str] = {}
    for module_name in _SYSTEM_CHAT_MODULE_ORDER:
        path = module_dir / f"{module_name}.md"
        if path.exists():
            try:
                modules[module_name] = path.read_text(encoding="utf-8").strip()
            except Exception as e:
                log.warning("Failed to read module %s: %s", module_name, e)
        else:
            log.debug("Module %s not found, skipping", module_name)

    if not modules:
        log.warning("No modules loaded — falling back to legacy")
        content = get_full_prompt("system_chat", "Chat")
        return content, {"system_chat": content}, "no modules loaded"

    assembled = "\n\n---\n\n".join(modules.values())
    return assembled, modules, None


def get_system_chat_prompt(modular: bool | None = None) -> str:
    """Convenience: get assembled system_chat prompt (modular or legacy)."""
    assembled, _, _ = get_system_chat_modules(modular)
    return assembled


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

    # Also include modular files
    module_dir = _PROMPTS_DIR / "system_chat"
    if module_dir.exists():
        for f in module_dir.glob("*.md"):
            name = f"system_chat/{f.stem}"
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

    # Include modular files
    module_dir = _PROMPTS_DIR / "system_chat"
    if module_dir.exists():
        names.extend(sorted(f"system_chat/{f.stem}" for f in module_dir.glob("*.md")))

    return names
