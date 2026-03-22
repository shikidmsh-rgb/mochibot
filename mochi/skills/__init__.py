"""Skill registry — auto-discovery and management of skills (v2).

Skills are discovered by scanning the skills/ directory for subdirectories
containing handler.py and SKILL.md.

Usage:
    from mochi.skills import discover, get_tools, dispatch
    discover()                        # scan and load all skills
    tools = get_tools()               # get all exposed tool definitions
    result = await dispatch(tool_name, args, user_id)

v2 additions:
    get_usage_rules_for_tools()       # collect usage rules for active tools
    get_by_trigger()                  # find skills by trigger config
    get_cron_skills()                 # return cron-scheduled skills
    skill_for_tool()                  # tool_name → skill_name lookup
    get_skill_info_all()              # admin metadata
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

            # Force SKILL.md loading to populate v2 attributes
            _ = skill.skill_md

            _skills[skill.name] = skill

            # Map tool names to skill
            if skill.expose_as_tool:
                for tool in skill.get_tools():
                    tool_name = tool.get("function", {}).get("name", "")
                    if tool_name:
                        _tool_map[tool_name] = skill.name

            registered.append(skill.name)
            log.info("Registered skill: %s (type=%s, tools=%s, triggers=%s)",
                     skill.name,
                     skill.skill_type,
                     [t["function"]["name"] for t in skill.get_tools()] if skill.get_tools() else "none",
                     skill.triggers)

        except Exception as e:
            log.error("Failed to load skill %s: %s", entry.name, e, exc_info=True)

    log.info("Skill discovery complete: %d skills registered", len(registered))
    return registered


# ---------------------------------------------------------------------------
# Core API (backward-compatible)
# ---------------------------------------------------------------------------

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


# Alias for consistency with private Mochi
skill_for_tool = get_tool_skill


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


# ---------------------------------------------------------------------------
# v2 API additions
# ---------------------------------------------------------------------------

def get_usage_rules_for_tools(tool_names: list[str]) -> str:
    """Collect usage rules from skills owning the given tools.

    Returns a concatenated string of all unique usage rules, or "".
    """
    seen_skills: set[str] = set()
    rules_parts: list[str] = []

    for tn in tool_names:
        sn = _tool_map.get(tn)
        if not sn or sn in seen_skills:
            continue
        seen_skills.add(sn)
        skill = _skills.get(sn)
        if skill and skill.usage_rules:
            rules_parts.append(f"### {skill.name}\n{skill.usage_rules}")

    return "\n\n".join(rules_parts) if rules_parts else ""


def get_by_trigger(trigger_type: str, **conditions) -> list[Skill]:
    """Find all skills that match a trigger type and optional conditions."""
    return [
        s for s in _skills.values()
        if s.has_trigger(trigger_type, **conditions)
    ]


def get_cron_skills() -> list[tuple[Skill, str]]:
    """Return cron-scheduled skills with their cron expressions.

    Returns: [(skill, "0 3 * * *"), ...] — only skills with type=cron triggers.
    """
    results = []
    for skill in _skills.values():
        for t in skill.triggers:
            if isinstance(t, dict) and t.get("type") == "cron":
                schedule = t.get("schedule", "")
                if schedule:
                    results.append((skill, schedule))
    return results


def get_skill_info_all() -> list[dict]:
    """Return metadata for all registered skills (for admin display)."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "type": s.skill_type,
            "expose_as_tool": s.expose_as_tool,
            "multi_turn": s.multi_turn,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.get_tools()] if s.get_tools() else [],
            "has_usage_rules": bool(s.usage_rules),
        }
        for s in _skills.values()
    ]


def list_skills() -> list[dict]:
    """List all registered skills with metadata (backward compat)."""
    return [
        {
            "name": s.name,
            "expose_as_tool": s.expose_as_tool,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.get_tools()] if s.get_tools() else [],
        }
        for s in _skills.values()
    ]
