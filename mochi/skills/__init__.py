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
    get_requestable_tool_lines()       # list unloaded requestable tools
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

    if skill.name == "image_generation":
        from mochi.image_service import get_image_config

        return [] if get_image_config()["configured"] else ["image model"]
    required = getattr(skill, "requires_config", [])
    if not required:
        return []
    from mochi.skill_config_resolver import resolve_skill_config
    fields = [field for field in skill._config_schema_typed if field.key in required]
    resolved = resolve_skill_config(skill.name, fields) if fields else {}
    db_config = get_skill_config(skill.name) if set(required) - resolved.keys() else {}
    return [
        key
        for key in required
        if not (
            resolved.get(key) if key in resolved
            else os.getenv(key) or skill.config.get(key) or db_config.get(key)
        )
    ]


_SKILLS_DIR = Path(__file__).parent

# Registries
_skills: dict[str, Skill] = {}           # name → skill instance
_tool_map: dict[str, str] = {}           # tool_name → skill_name
_prompt_hooks: dict[str, Skill] = {}     # skill_name → skill (has prompt_section)
_external_discovered = False
_external_errors: dict[str, str] = {}


class _ExtensionMetadata(Skill):
    """Read-only management metadata, never registered for execution."""

    async def execute(self, context: SkillContext) -> SkillResult:
        raise RuntimeError("Extension metadata is not executable")


def _validate_registration(skill: Skill, *, replace: bool = False) -> list[str]:
    existing = _skills.get(skill.name)
    if existing and (existing.external or skill.external) and not (
        replace and existing.external and skill.external
    ):
        raise ValueError(f"Duplicate skill name '{skill.name}'")
    tool_names = [tool["function"]["name"] for tool in skill.get_tools()]
    if len(tool_names) != len(set(tool_names)):
        raise ValueError(f"Duplicate tool names in '{skill.name}'")
    for tool_name in tool_names:
        owner = _tool_map.get(tool_name)
        if owner and owner != skill.name:
            raise ValueError(
                f"Duplicate tool name '{tool_name}' in skills "
                f"'{owner}' and '{skill.name}'"
            )
        if tool_name in {"request_tools", "enter_bedtime"}:
            raise ValueError(f"Reserved framework tool '{tool_name}'")
    return tool_names


def _register_skill(skill: Skill, *, replace: bool = False) -> None:
    """Publish a complete registration only after checking ownership."""
    tool_names = _validate_registration(skill, replace=replace)
    if replace:
        for name, owner in list(_tool_map.items()):
            if owner == skill.name:
                del _tool_map[name]
    _skills[skill.name] = skill
    _tool_map.update(dict.fromkeys(tool_names, skill.name))
    if not skill.external and callable(getattr(skill, "prompt_section", None)):
        _prompt_hooks[skill.name] = skill


def _load_external(name: str, *, activate: bool = False) -> Skill:
    from mochi.extensions import loader, store
    from mochi.skill_config_resolver import resolve_skill_config

    entered_code = False
    try:
        with store._operation():
            if name in _get_disabled_skills():
                raise store.ExtensionError("extension_disabled", "Enable this extension before activating it.")
            root = store.extension_root(name)
            package = root / ("draft" if activate else "current")
            metadata = _ExtensionMetadata()
            metadata.external = True
            metadata._populate_from_md(loader.read_metadata(name, package))
            config = resolve_skill_config(name, metadata._config_schema_typed)
            metadata.config = config
            missing = get_missing_config(metadata)
            if activate and missing:
                raise store.ExtensionError("missing_config", f"Configure these keys before activation: {', '.join(missing)}")
            loader.validate_package(name, package)
            entered_code = True
            skill = loader.load_snapshot(name, package, data_dir=root / "data", config=config)
            _validate_registration(skill, replace=True)
            if activate:
                store.publish(name, Path(skill.__module_file__).parent)
            _register_skill(skill, replace=True)
            _external_errors.pop(name, None)
    except Exception as exc:
        if name not in _skills:
            _external_errors[name] = str(exc)
        log.exception("Personal extension %s failed: %s", name, exc)
        raise store.ExtensionError(
            getattr(exc, "code", "extension_load_failed"),
            f"{exc}. The running registration has not been replaced.",
            state_changed=bool(getattr(exc, "state_changed", False)),
            state_change_unknown=entered_code or bool(getattr(exc, "state_change_unknown", False)),
        ) from exc
    return skill


def activate_extension(name: str) -> dict:
    skill = _load_external(name, activate=True)
    return {
        "name": name, "active": True, "persisted": True,
        "tools": sorted(skill.tool_names()),
        "request_tools": {"skills": [name]},
        "message": "Active now. Request this namespace for subsequent provider rounds; no restart is needed.",
        "mod_api": dict(skill._mod_api),
        "state_changed": True,
    }


def load_installed_extension(name: str) -> bool:
    """Load enabled installed code explicitly; a draft alone is not activation."""
    from mochi.extensions import store

    if get_skill(name) is not None:
        return True
    package = next((p for p in store.list_extensions() if p["name"] == name), None)
    if not package or not package["installed"]:
        return False
    _load_external(name)
    return True


def _discover_external() -> list[str]:
    global _external_discovered
    if _external_discovered:
        return []
    _external_discovered = True

    from mochi.extensions import store

    disabled = _get_disabled_skills()
    registered = []
    for package in store.list_extensions():
        name = package["name"]
        if name in disabled or not package["installed"]:
            continue
        try:
            _load_external(name)
        except store.ExtensionError:
            continue
        else:
            registered.append(name)
            log.info("Registered personal extension: %s", name)
    return registered


def get_skill_for_management(name: str) -> Skill | None:
    """Read metadata for an unloaded package without importing its handler."""
    if name in _skills:
        return _skills[name]
    from mochi.extensions import loader, store
    from mochi.skill_config_resolver import resolve_skill_config

    package = next((p for p in store.list_extensions() if p["name"] == name), None)
    if package is None:
        return None
    skill = _ExtensionMetadata()
    skill._name = name
    skill.external = True
    skill.description = "Personal extension"
    try:
        root = store.extension_root(name)
        area = "current" if package["installed"] else "draft"
        parsed = loader.read_metadata(name, root / area)
        skill._skill_md = parsed
        skill._populate_from_md(parsed)
        skill.config = resolve_skill_config(name, skill._config_schema_typed)
    except (OSError, ValueError) as exc:
        skill._skill_md = {"tools": []}
        skill._metadata_error = str(exc)
    return skill


def get_skill_configuration(name: str) -> Skill | None:
    """Expose draft configuration without changing live execution metadata."""
    skill = get_skill_for_management(name)
    if skill is None or not skill.external:
        return skill
    from mochi.extensions import loader, store
    from mochi.skill_config_resolver import resolve_skill_config

    try:
        draft = store._area(name, "draft", required=False)
        if not draft.is_dir():
            return skill
        view = _ExtensionMetadata()
        view.external = True
        view._populate_from_md(loader.read_metadata(name, draft))
    except (OSError, ValueError) as exc:
        log.warning("Draft configuration unavailable for %s; using installed metadata: %s", name, exc)
        return skill
    typed = {field.key: field for field in skill._config_schema_typed}
    typed.update({field.key: field for field in view._config_schema_typed})
    fields = {field["key"]: field for field in skill.config_schema}
    fields.update({field["key"]: field for field in view.config_schema})
    view._config_schema_typed = list(typed.values())
    view.config_schema = list(fields.values())
    view.requires_config = sorted(set(skill.requires_config) | set(view.requires_config))
    view.config = resolve_skill_config(name, view._config_schema_typed)
    return view


def refresh_skill_configuration(name: str) -> None:
    skill = get_skill(name)
    if skill is not None:
        skill.refresh_config()


def init_all_skill_schemas() -> None:
    """Call init_schema() on every registered skill.

    Must be called after discover() so that _skills is populated, and
    after init_db() so that framework tables exist.  Each skill gets its
    own DB connection so a single failure doesn't affect others.
    """
    from mochi.db import _connect

    for name, skill in _skills.items():
        if skill.external:
            continue
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

            _register_skill(skill)

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

    registered.extend(_discover_external())
    log.info("Skill discovery complete: %d skills registered", len(registered))
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


def get_declared_tools() -> list[dict]:
    """Return contracts without changing eligibility or importing inactive code."""
    return [tool for skill in _skills.values() for tool in skill.get_tools()]


def get_effective_tools_for_skill(
    skill_name: str, *, available: bool = True, states: dict | None = None,
) -> list[dict]:
    from mochi.adaptive_tool_load import resolve_definition
    from mochi.db import get_adaptive_tool_load_states

    skill = _skills.get(skill_name)
    if skill is None:
        return []
    if states is None:
        states = get_adaptive_tool_load_states()
    definitions = skill.available_tools() if available else skill.get_tools()
    return [resolve_definition(tool, states=states) for tool in definitions]


def get_tools(transport: str = "") -> list[dict]:
    """Get every eligible registered tool definition."""
    disabled = _get_disabled_skills()
    from mochi.db import get_adaptive_tool_load_states
    states = get_adaptive_tool_load_states()
    tools = []
    for skill in _skills.values():
        if skill.name in disabled:
            continue
        if get_missing_config(skill):
            continue
        if transport and transport in skill.exclude_transports:
            continue
        tools.extend(get_effective_tools_for_skill(skill.name, states=states))
    return tools


def get_tools_by_names(
    skill_names: list[str],
    transport: str = "",
    loads: set[str] | frozenset[str] | None = None,
) -> list[dict]:
    """Get eligible tools for named skills, optionally filtered by load."""
    disabled = _get_disabled_skills()
    from mochi.db import get_adaptive_tool_load_states
    states = get_adaptive_tool_load_states()
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
        for tool in get_effective_tools_for_skill(name, states=states):
            if loads is not None and tool.get("_load") not in loads:
                continue
            tools.append(tool)
    return tools


def get_tools_by_load(load: str, transport: str = "") -> list[dict]:
    """Get eligible tools with one effective load in stable registry order."""
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
    from mochi.db import get_adaptive_tool_load_states
    states = get_adaptive_tool_load_states()
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
                for tool in get_effective_tools_for_skill(skill_name, states=states)
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
                   owner_authorized: bool = False,
                   source: str = "",
                   turn_id: str = "",
                   bound_skill: Skill | None = None) -> SkillResult:
    """Dispatch a tool call to the appropriate skill."""
    skill_name = bound_skill.name if bound_skill else _tool_map.get(tool_name)
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

    skill = bound_skill or _skills.get(skill_name)
    if not skill:
        return SkillResult(
            output=f"Skill not found: {skill_name}",
            success=False,
            error_code="skill_not_found",
            retryable=False,
        )

    if not skill.handles(tool_name):
        return SkillResult(output=f"Unknown tool: {tool_name}", success=False, error_code="unknown_tool")
    if bound_skill is not None:
        skill.refresh_config()
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
        source=source,
        turn_id=turn_id,
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

def get_capability_context_for_tools(tool_names: list[str]) -> str:
    """Collect capability context for skills represented in ``tool_names``.

    Returns facts, deterministic effects, and hard boundaries from each
    skill's SKILL.md. Legacy directive sections are never included.
    """
    seen_skills: set[str] = set()
    context_parts: list[str] = []

    for tn in tool_names:
        sn = _tool_map.get(tn)
        if not sn or sn in seen_skills:
            continue
        seen_skills.add(sn)
        skill = _skills.get(sn)
        if skill and skill.capability_context:
            context_parts.append(f"#### {skill.name}\n{skill.capability_context}")

    return "\n\n".join(context_parts)


def get_requestable_tool_lines(
    tool_names: list[str],
    *,
    transport: str = "",
    excluded_skills: frozenset[str] = frozenset(),
) -> str:
    """List requestable tools that are not loaded, grouped by skill name.

    Their capability context is returned by request_tools when loaded.
    """
    from mochi.request_tools import build_catalog

    tool_set = set(tool_names)
    catalog = build_catalog(transport=transport, excluded_skills=excluded_skills)
    lines = []
    for skill_name, namespace in catalog.eligible.items():
        unloaded = [name for name in namespace.tool_names if name not in tool_set]
        if unloaded:
            lines.append(f"- {skill_name}: {', '.join(unloaded)}")
    return "\n".join(lines)


def get_skill_info_all() -> list[dict]:
    """Return live skills and metadata-only personal packages for management."""
    from mochi.extensions import loader, store
    from mochi.adaptive_tool_load import resolve_definition
    from mochi.db import get_adaptive_tool_load_states

    load_states = get_adaptive_tool_load_states()
    disabled = _get_disabled_skills()
    packages = {p["name"]: p for p in store.list_extensions()}
    skills = dict(_skills)
    for name in packages.keys() - skills.keys():
        metadata = get_skill_for_management(name)
        if metadata:
            skills[name] = metadata
    result = []
    for s in skills.values():
        loaded = s.name in _skills
        load_error = (
            _external_errors.get(s.name)
            or packages.get(s.name, {}).get("error")
            or getattr(s, "_metadata_error", "")
        )
        config_missing = get_missing_config(s)
        configuration = get_skill_configuration(s.name) if s.external else s
        if configuration is None:
            configuration = s
        admin_disabled = s.name in disabled
        auto_disabled = bool(config_missing) or not loaded
        mod_api = {}
        if s.external:
            active_api = getattr(s, "_mod_api", None) if loaded else None
            mod_api["active"] = dict(active_api) if active_api is not None else None
            package = packages.get(s.name, {})
            for area, present in (
                ("draft", package.get("has_draft")), ("current", package.get("installed")),
            ):
                if not present:
                    continue
                try:
                    mod_api[area] = loader.inspect_mod_api(store.extension_root(s.name) / area)
                except (OSError, ValueError) as exc:
                    mod_api[area] = {
                        "declared": None, "effective": None,
                        "status": "unavailable", "error": str(exc)[:1000],
                    }
        effective_definitions = [
            resolve_definition(tool, states=load_states) for tool in s.get_tools()
        ]
        result.append({
            **packages.get(s.name, {}),
            "name": s.name,
            "description": s.description,
            "type": s.skill_type,
            "multi_turn": s.multi_turn,
            "triggers": s.triggers,
            "tools": [t["function"]["name"] for t in s.available_tools()],
            "tool_loads": [
                {
                    "name": tool["function"]["name"],
                    "declared": tool.get("_declared_load", tool.get("_load")),
                    "effective": tool.get("_load"),
                    "adaptive": bool(tool.get("_adaptive_load")),
                    "pinned": tool.get("_load_pinned"),
                    "reason": tool.get("_load_reason", ""),
                    "changed_at": tool.get("_load_changed_at"),
                }
                for tool in effective_definitions
            ],
            "has_capability_context": bool(s.capability_context),
            "requires_config": getattr(s, "requires_config", []),
            "config_required": configuration.requires_config,
            "enabled": not admin_disabled and not auto_disabled,
            "admin_disabled": admin_disabled,
            "auto_disabled": auto_disabled,
            "config_status": {
                **{key: bool(os.getenv(key) or configuration.config.get(key))
                   for key in configuration.requires_config},
                **{entry["key"]: bool(configuration.config.get(entry["key"]))
                   for entry in configuration.config_schema},
            },
            "has_observer": s.has_observer,
            "locked": getattr(s, "locked", False),
            "diary_tags": s.diary_tags,
            "config_missing": config_missing,
            "config_schema": configuration.config_schema,
            "sub_skills": s.sub_skills,
            "exclude_transports": s.exclude_transports,
            "source": "personal" if s.external else "bundled",
            "loaded": loaded,
            "load_error": load_error,
            "activation_required": s.external and not loaded,
            **({"mod_api": mod_api} if s.external else {}),
            **(
                {"development_enabled": "development" not in disabled}
                if s.name == "personal_workspace" else {}
            ),
        })
    return result
