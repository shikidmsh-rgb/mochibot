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
from datetime import datetime
from pathlib import Path

from mochi.skills.base import Skill, SkillContext, SkillResult

log = logging.getLogger(__name__)


def _get_disabled_skills() -> set[str]:
    """Lazy wrapper to avoid circular import with mochi.db."""
    from mochi.db import get_disabled_skills
    return get_disabled_skills()

_SKILLS_DIR = Path(__file__).parent

# Registries
_skills: dict[str, Skill] = {}           # name → skill instance
_tool_map: dict[str, str] = {}           # tool_name → skill_name


def init_all_skill_schemas() -> None:
    """Call init_schema() on every registered skill.

    Must be called after discover() so that _skills is populated, and
    after init_db() so that framework tables exist.  Each skill gets its
    own DB connection so a single failure doesn't affect others.
    """
    from mochi.db import _connect

    for name, skill in _skills.items():
        try:
            conn = _connect()
            skill.init_schema(conn)
            conn.commit()
            conn.close()
        except Exception:
            log.exception("init_schema failed for skill %s", name)


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

            # Force SKILL.md loading to populate v2/v3 attributes
            _ = skill.skill_md

            # Resolve config from priority chain (DB > env > schema default)
            if skill._config_schema_typed:
                try:
                    from mochi.skill_config_resolver import resolve_skill_config
                    skill.config = resolve_skill_config(skill.name, skill._config_schema_typed)
                except Exception as e:
                    log.warning("Skill %s config resolution failed: %s", skill.name, e)

            # Check required config vars (parity with observer auto-disable)
            # Check both os.environ AND DB-resolved skill.config (admin portal
            # saves to DB, not .env, so os.getenv alone misses DB values).
            missing_config = [
                key for key in skill.requires_config
                if not os.getenv(key) and not skill.config.get(key)
            ]
            if missing_config:
                log.info(
                    "Skill %s config incomplete — missing: %s",
                    skill.name, missing_config,
                )
                skill._config_missing = missing_config
            else:
                skill._config_missing = []

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
    refresh_capability_summary()
    return registered


# ---------------------------------------------------------------------------
# Core API (backward-compatible)
# ---------------------------------------------------------------------------

def get_skill(name: str) -> Skill | None:
    """Get a skill by name."""
    return _skills.get(name)


def collect_diary_status(user_id: int, today: str, now: datetime) -> list[str]:
    """Collect diary status lines from all enabled skills.

    Iterates registered skills in diary_status_order, calls diary_status()
    on each enabled skill, collects lines.  One skill's failure never affects
    others.
    """
    if not _skills:
        return []
    disabled = _get_disabled_skills()
    ordered = sorted(
        _skills.values(),
        key=lambda s: (s.diary_status_order, s.name),
    )
    all_lines: list[str] = []
    for skill in ordered:
        if skill.name in disabled:
            continue
        if getattr(skill, "_config_missing", None):
            continue
        try:
            lines = skill.diary_status(user_id, today, now)
            if lines:
                all_lines.extend(lines)
        except Exception:
            log.exception("diary_status failed for skill %s", skill.name)
    return all_lines


def get_tools(transport: str = "") -> list[dict]:
    """Get all exposed tool definitions (for LLM tools array).

    Excludes tools from admin-disabled, config-missing, or
    transport-incompatible skills.
    """
    disabled = _get_disabled_skills()
    tools = []
    for skill in _skills.values():
        if skill.name in disabled:
            continue
        if getattr(skill, "_config_missing", None):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        if skill.expose_as_tool:
            tools.extend(skill.get_tools())
    return tools


def get_tools_by_names(skill_names: list[str],
                       transport: str = "") -> list[dict]:
    """Get tool definitions for tools belonging to named skills.

    Ignores expose_as_tool — if you ask by name, you get it.
    This is intentional: expose_as_tool controls the *default* full injection
    (get_tools), but pre-router already classified the message and decided
    these skills are needed, so we honour the request.

    Invalid names are silently skipped (logged at debug level).
    Skips skills with missing required config or transport exclusion.
    """
    tools = []
    for name in skill_names:
        skill = _skills.get(name)
        if not skill:
            log.debug("get_tools_by_names: unknown skill %s, skipped", name)
            continue
        if getattr(skill, "_config_missing", None):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        tools.extend(skill.get_tools())
    return tools


def get_tool_skill(tool_name: str) -> str | None:
    """Get the skill name that owns a tool."""
    return _tool_map.get(tool_name)


# Alias for consistency with private Mochi
skill_for_tool = get_tool_skill


async def dispatch(tool_name: str, args: dict, user_id: int = 0,
                   channel_id: int = 0, transport: str = "") -> SkillResult:
    """Dispatch a tool call to the appropriate skill."""
    skill_name = _tool_map.get(tool_name)
    if not skill_name:
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False)

    if skill_name in _get_disabled_skills():
        return SkillResult(output=f"Skill '{skill_name}' is currently disabled.", success=False)

    skill = _skills.get(skill_name)
    if not skill:
        return SkillResult(output=f"Skill not found: {skill_name}", success=False)

    if getattr(skill, "_config_missing", None):
        return SkillResult(output=f"Skill '{skill_name}' is unavailable (missing config).", success=False)

    if transport and transport in skill.exclude_transports:
        return SkillResult(
            output=f"Skill '{skill_name}' is not available on this platform.",
            success=False,
        )

    context = SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=channel_id,
        transport=transport,
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
    disabled = _get_disabled_skills()
    result = []
    for s in _skills.values():
        # Re-check config at call time (DB values may have been added since
        # discovery, so the stale _config_missing from startup can be wrong).
        try:
            from mochi.db import get_skill_config as _gsc
            db_cfg = _gsc(s.name)
        except Exception:
            db_cfg = {}
        config_missing = [
            key for key in getattr(s, "requires_config", [])
            if not os.getenv(key) and not s.config.get(key) and not db_cfg.get(key)
        ]
        admin_disabled = s.name in disabled
        auto_disabled = bool(config_missing)
        result.append({
            "name": s.name,
            "description": s.description,
            "type": s.skill_type,
            "tier": s.tier,
            "expose_as_tool": s.expose_as_tool,
            "multi_turn": s.multi_turn,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.get_tools()] if s.get_tools() else [],
            "has_usage_rules": bool(s.usage_rules),
            "requires_config": getattr(s, "requires_config", []),
            "enabled": not admin_disabled and not auto_disabled,
            "admin_disabled": admin_disabled,
            "auto_disabled": auto_disabled,
            "config_status": {
                **{key: bool(os.getenv(key) or s.config.get(key))
                   for key in getattr(s, "requires_config", [])},
                **{entry["key"]: entry["key"] in s.config and bool(s.config[entry["key"]])
                   for entry in s.config_schema},
            },
            "has_observer": s.has_observer,
            "core": getattr(s, "core", False),
            "diary_tags": s.diary_tags,
            "config_missing": config_missing,
            "config_schema": s.config_schema,
            "sub_skills": s.sub_skills,
            "exclude_transports": s.exclude_transports,
        })
    return result


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


# ---------------------------------------------------------------------------
# Dynamic capability summary (for system prompt)
# ---------------------------------------------------------------------------

_capability_summary: dict[str, str] = {}


def _build_capability_summary(transport: str = "") -> str:
    """Build a Chinese markdown section listing currently available skills.

    Filters:
    - Excludes admin-disabled skills
    - Excludes skills with missing required config
    - Excludes type=automation (internal, e.g. maintenance)
    - Excludes skills incompatible with the given transport (noted separately)
    """
    disabled = _get_disabled_skills()
    lines: list[str] = []
    excluded_names: list[str] = []

    for s in _skills.values():
        if s.name in disabled:
            continue
        if getattr(s, "_config_missing", None):
            continue
        if s.skill_type == "automation":
            continue
        if transport and transport in s.exclude_transports:
            excluded_names.append(s.description or s.name)
            continue
        if s.description:
            lines.append(f"- {s.description}")

    if excluded_names:
        lines.append(f"- (此平台不可用: {', '.join(excluded_names)})")

    if not lines:
        return ""
    return "### 当前技能\n" + "\n".join(lines)


def get_capability_summary(transport: str = "") -> str:
    """Return cached capability summary for system prompt injection."""
    global _capability_summary
    if transport not in _capability_summary:
        _capability_summary[transport] = _build_capability_summary(transport)
    return _capability_summary[transport]


def refresh_capability_summary() -> None:
    """Rebuild the cached capability summary (call after skill toggle/config change)."""
    global _capability_summary
    _capability_summary = {}
