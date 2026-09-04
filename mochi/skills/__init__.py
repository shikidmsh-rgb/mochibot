"""Skill registry — auto-discovery and management of skills (v2).

Skills are discovered by scanning the skills/ directory for subdirectories
containing handler.py and SKILL.md.

Usage:
    from mochi.skills import discover, get_tools, dispatch
    discover()                        # scan and load all skills
    tools = get_tools()               # get all exposed tool definitions
    result = await dispatch(tool_name, args, user_id)

Additional APIs:
    get_capability_context_for_tools() # collect capability facts for active tools
    skill_for_tool()                   # tool_name → skill_name lookup
    get_skill_info_all()               # admin metadata
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

    return {
        name
        for name in get_disabled_skills()
        if not getattr(_skills.get(name), "locked", False)
    }


def get_missing_config(skill: Skill) -> list[str]:
    """Return currently missing required config using live DB values."""
    from mochi.db import get_skill_config

    db_config = get_skill_config(skill.name)
    return [
        key
        for key in getattr(skill, "requires_config", [])
        if not os.getenv(key)
        and not skill.config.get(key)
        and not db_config.get(key)
    ]


_SKILLS_DIR = Path(__file__).parent

# Registries
_skills: dict[str, Skill] = {}           # name → skill instance
_tool_map: dict[str, str] = {}           # tool_name → skill_name
_prompt_hooks: dict[str, Skill] = {}     # skill_name → skill (has prompt_section)


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

            # Map every declared tool name to exactly one owning skill.
            for tool in skill.get_tools():
                tool_name = tool.get("function", {}).get("name", "")
                if tool_name:
                    existing_owner = _tool_map.get(tool_name)
                    if existing_owner and existing_owner != skill.name:
                        raise ValueError(
                            f"Duplicate tool name '{tool_name}' in skills "
                            f"'{existing_owner}' and '{skill.name}'"
                        )
                    _tool_map[tool_name] = skill.name

            # Register prompt section hook if skill provides one
            if hasattr(skill, 'prompt_section') and callable(skill.prompt_section):
                _prompt_hooks[skill.name] = skill

            registered.append(skill.name)
            log.info("Registered skill: %s (type=%s, tools=%s, triggers=%s)",
                     skill.name,
                     skill.skill_type,
                     [t["function"]["name"] for t in skill.get_tools()] if skill.get_tools() else "none",
                     skill.triggers)

        except Exception as e:
            log.error("Failed to load skill %s: %s", entry.name, e, exc_info=True)
            if isinstance(e, ValueError):
                raise

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
        if get_missing_config(skill):
            continue
        try:
            lines = skill.diary_status(user_id, today, now)
            if lines:
                all_lines.extend(lines)
        except Exception:
            log.exception("diary_status failed for skill %s", skill.name)
    return all_lines


def get_tools(transport: str = "") -> list[dict]:
    """Get every eligible registered tool definition."""
    disabled = _get_disabled_skills()
    tools = []
    for skill in _skills.values():
        if skill.name in disabled:
            continue
        if get_missing_config(skill):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        tools.extend(skill.get_tools())
    return tools


def get_tools_by_names(
    skill_names: list[str],
    transport: str = "",
    loads: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Get eligible tools for named skills, optionally filtered by load."""
    disabled = _get_disabled_skills()
    tools = []
    for name in skill_names:
        skill = _skills.get(name)
        if not skill:
            log.warning("get_tools_by_names: unknown skill %s, skipped", name)
            continue
        if name in disabled:
            continue
        if get_missing_config(skill):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        for tool in skill.get_tools():
            if loads is not None and tool.get("_load") not in loads:
                continue
            tools.append(tool)
    return tools


def get_tools_by_load(load: str, transport: str = "") -> list[dict]:
    """Get eligible tools with one declared load in stable registry order."""
    skill_names = list(_skills)
    if load == "resident":
        # Preserve the provider-schema order used before metadata discovery:
        # lifecycle-locked residents, transport-specific residents, then the
        # remaining residents. Declaration order is stable within each group.
        skill_names.sort(key=lambda name: (
            not _skills[name].locked,
            not bool(_skills[name].exclude_transports),
        ))
    return get_tools_by_names(
        skill_names,
        transport=transport,
        loads={load},
    )


def get_tools_by_tool_names(
    tool_names: list[str] | tuple[str, ...],
    transport: str = "",
) -> list[dict]:
    """Get live definitions for exact tool names without loading siblings."""
    disabled = _get_disabled_skills()
    tools: list[dict] = []
    for tool_name in tool_names:
        skill_name = _tool_map.get(tool_name)
        skill = _skills.get(skill_name or "")
        if not skill or skill_name in disabled:
            continue
        if get_missing_config(skill):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        definition = next(
            (
                tool
                for tool in skill.get_tools()
                if tool.get("function", {}).get("name") == tool_name
            ),
            None,
        )
        if definition is not None:
            tools.append(definition)
    return tools


def get_tool_skill(tool_name: str) -> str | None:
    """Get the skill name that owns a tool."""
    return _tool_map.get(tool_name)


# Legacy alias
skill_for_tool = get_tool_skill


async def dispatch(tool_name: str, args: dict, user_id: int = 0,
                   channel_id: int = 0, transport: str = "",
                   actor: str = "",
                   owner_authorized: bool = False) -> SkillResult:
    """Dispatch a tool call to the appropriate skill."""
    skill_name = _tool_map.get(tool_name)
    if not skill_name:
        return SkillResult(
            output=f"Unknown tool: {tool_name}",
            success=False,
            error_code="unknown_tool",
            retryable=False,
        )

    if skill_name in _get_disabled_skills():
        return SkillResult(
            output=f"Skill '{skill_name}' is currently disabled.",
            success=False,
            error_code="skill_disabled",
            retryable=False,
        )

    skill = _skills.get(skill_name)
    if not skill:
        return SkillResult(
            output=f"Skill not found: {skill_name}",
            success=False,
            error_code="skill_not_found",
            retryable=False,
        )

    if get_missing_config(skill):
        return SkillResult(
            output=f"Skill '{skill_name}' is unavailable (missing config).",
            success=False,
            error_code="missing_config",
            retryable=False,
        )

    if transport and transport in skill.exclude_transports:
        return SkillResult(
            output=f"Skill '{skill_name}' is not available on this platform.",
            success=False,
            error_code="transport_unavailable",
            retryable=False,
        )

    context = SkillContext(
        trigger="tool_call",
        user_id=user_id,
        channel_id=channel_id,
        transport=transport,
        actor=actor,
        owner_authorized=owner_authorized,
        tool_name=tool_name,
        args=args,
    )

    return await skill.run(context)


def all_skills() -> dict[str, Skill]:
    """Return the full skill registry (read-only snapshot)."""
    return dict(_skills)


def get_prompt_sections(compact: bool = False) -> list[str]:
    """Collect system prompt sections from skills that declare prompt_section().

    Returns list of formatted section strings. Respects skill enabled state.
    """
    disabled = _get_disabled_skills()
    sections: list[str] = []
    for name, skill in _prompt_hooks.items():
        if name in disabled:
            continue
        if get_missing_config(skill):
            continue
        try:
            section = skill.prompt_section(compact=compact)
            if section:
                sections.append(section)
        except Exception:
            log.warning("prompt_section failed for skill %s", name)
    return sections


# ---------------------------------------------------------------------------
# v2 API additions
# ---------------------------------------------------------------------------

def get_capability_context_for_tools(
    tool_names: list[str],
    *,
    include_requestable_tools: bool = False,
    transport: str = "",
) -> str:
    """Collect Main-facing capability context for the given tools.

    For each skill represented in ``tool_names``, also lists any on-demand
    tools belonging to that skill that are NOT currently loaded — so the LLM
    knows what's reachable via ``request_tools``.

    Returns facts, deterministic effects, and hard boundaries from each
    represented skill. Legacy directive sections are never included.
    """
    seen_skills: set[str] = set()
    context_parts: list[str] = []
    tool_set = set(tool_names)

    for tn in tool_names:
        sn = _tool_map.get(tn)
        if not sn or sn in seen_skills:
            continue
        seen_skills.add(sn)
        skill = _skills.get(sn)
        if not skill or not skill.capability_context:
            continue
        block = f"### {skill.name}\n{skill.capability_context}"

        on_demand_not_loaded = sorted(
            tool["function"]["name"]
            for tool in skill.get_tools()
            if tool.get("_load") == "on_demand"
            and tool["function"]["name"] not in tool_set
        )
        if include_requestable_tools and on_demand_not_loaded:
            block += (
                f"\n(可通过 request_tools 申请的按需工具: "
                f"{', '.join(on_demand_not_loaded)})"
            )
        context_parts.append(block)

    if include_requestable_tools:
        from mochi.request_tools import build_catalog

        catalog = build_catalog(transport=transport)
        for skill_name, namespace in catalog.eligible.items():
            if skill_name in seen_skills:
                continue
            skill = _skills.get(skill_name)
            if not skill or not skill.capability_context:
                continue
            seen_skills.add(skill_name)
            context_parts.append(
                f"### {skill.name}\n{skill.capability_context}\n"
                f"(可通过 request_tools 申请: {', '.join(namespace.tool_names)})"
            )

    return "\n\n".join(context_parts) if context_parts else ""


def get_skill_info_all() -> list[dict]:
    """Return metadata for all registered skills (for admin display)."""
    disabled = _get_disabled_skills()
    result = []
    for s in _skills.values():
        config_missing = get_missing_config(s)
        admin_disabled = s.name in disabled
        auto_disabled = bool(config_missing)
        result.append({
            "name": s.name,
            "description": s.description,
            "type": s.skill_type,
            "multi_turn": s.multi_turn,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.get_tools()] if s.get_tools() else [],
            "has_capability_context": bool(s.capability_context),
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
            "locked": getattr(s, "locked", False),
            "diary_tags": s.diary_tags,
            "config_missing": config_missing,
            "config_schema": s.config_schema,
            "sub_skills": s.sub_skills,
            "exclude_transports": s.exclude_transports,
        })
    return result


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
        if get_missing_config(s):
            continue
        if s.skill_type == "automation":
            continue
        if transport and transport in s.exclude_transports:
            excluded_names.append(s.description or s.name)
            continue
        if s.description:
            short = s.description.split("—")[0].strip(" \"") if "—" in s.description else s.description
            lines.append(f"- {short}")

    if excluded_names:
        lines.append(f"- (此平台不可用: {', '.join(excluded_names)})")

    if not lines:
        return ""
    return "### 你的技能\n" + "\n".join(lines)


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
