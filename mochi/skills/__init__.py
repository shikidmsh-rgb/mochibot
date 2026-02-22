"""Skill registry — auto-discovery and management of skills.

Skills are discovered by scanning the skills/ directory for subdirectories
containing handler.py and SKILL.md.

Usage:
    from mochi.skills import registry
    registry.discover()           # scan and load all skills
    tools = registry.get_tools()  # get all exposed tool definitions
    result = await registry.dispatch(tool_name, args, user_id)
"""

import importlib
import logging
import os
from pathlib import Path

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent

# Registries
_skills: dict[str, Skill] = {}           # name → skill instance
_tool_map: dict[str, str] = {}           # tool_name → skill_name


def discover() -> list[str]:
    """Scan the skills directory and register all valid skills.

    A valid skill has: __init__.py + handler.py + SKILL.md
    Returns list of registered skill names.
    """
    registered = []

    for entry in sorted(_SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue

        handler_path = entry / "handler.py"
        skill_md_path = entry / "SKILL.md"

        if not handler_path.exists():
            continue

        # Skip disabled skills
        if not skill_md_path.exists() and (entry / "SKILL.md.disabled").exists():
            log.info("Skill disabled: %s", entry.name)
            continue

        try:
            module = importlib.import_module(f"mochi.skills.{entry.name}.handler")
            # Look for a class that subclasses Skill
            skill_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type) and issubclass(attr, Skill)
                        and attr is not Skill):
                    skill_cls = attr
                    break

            if skill_cls is None:
                log.warning("No Skill subclass found in %s", entry.name)
                continue

            skill = skill_cls()
            _skills[skill.name] = skill

            # Map tool names to skill
            if skill.expose_as_tool:
                for tool in skill.get_tools():
                    tool_name = tool.get("function", {}).get("name", "")
                    if tool_name:
                        _tool_map[tool_name] = skill.name

            registered.append(skill.name)
            log.info("✅ Registered skill: %s (tools: %s, triggers: %s)",
                     skill.name,
                     [t["function"]["name"] for t in skill.get_tools()] if skill.get_tools() else "none",
                     skill.triggers)

        except Exception as e:
            log.error("Failed to load skill %s: %s", entry.name, e, exc_info=True)

    log.info("Skill discovery complete: %d skills registered", len(registered))
    return registered


def get_skill(name: str) -> Skill | None:
    """Get a skill by name."""
    return _skills.get(name)


def get_tools() -> list[dict]:
    """Get all exposed tool definitions (for LLM tools array)."""
    tools = []
    for skill in _skills.values():
        if skill.expose_as_tool:
            tools.extend(skill.get_tools())
    return tools


def get_tool_skill(tool_name: str) -> str | None:
    """Get the skill name that owns a tool."""
    return _tool_map.get(tool_name)


async def dispatch(tool_name: str, args: dict, user_id: int = 0,
                   channel_id: int = 0) -> SkillResult:
    """Dispatch a tool call to the appropriate skill."""
    skill_name = _tool_map.get(tool_name)
    if not skill_name:
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False)

    skill = _skills.get(skill_name)
    if not skill:
        return SkillResult(output=f"Skill not found: {skill_name}", success=False)

    context = SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=channel_id,
        tool_name=tool_name,
        args=args,
    )

    return await skill.run(context)


def list_skills() -> list[dict]:
    """List all registered skills with metadata."""
    return [
        {
            "name": s.name,
            "expose_as_tool": s.expose_as_tool,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.get_tools()] if s.get_tools() else [],
        }
        for s in _skills.values()
    ]
