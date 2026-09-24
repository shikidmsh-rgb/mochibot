"""Compute the tool policy for one chat turn."""

from __future__ import annotations

import time
from dataclasses import dataclass

import mochi.skills as skill_registry
from mochi.request_tools import REQUEST_TOOLS_DEF
from mochi.tool_policy import filter_tools


_SKILLOFF_BASE_TOOLS = ("update_core", "manage_agent_settings")
_SKILLOFF_TELEGRAM_TOOLS = ("send_sticker",)
MAX_ROUTED_SKILLS = 2

# Owner chat toolbox kept in memory: {user_id: (turn_started, reset_at, names)}.
# A restart or conversation reset simply starts a new toolbox.
_session_toolboxes: dict[int, tuple[float, str | None, tuple[str, ...]]] = {}


@dataclass(frozen=True)
class TurnToolPlan:
    """Resolved tool mode and definitions for one incoming message."""

    pure_chat: bool
    router_enabled: bool
    router_status: str
    request_tools_enabled: bool
    resident_definitions: tuple[dict, ...]
    router_catalog: tuple[tuple[str, str], ...]

    @property
    def router_descriptions(self) -> dict[str, str]:
        return dict(self.router_catalog)

    def filter_router_selection(self, skill_names: object) -> list[str]:
        """Keep at most two unique, currently eligible daily skills."""
        if not self.router_enabled or not isinstance(skill_names, list):
            return []
        eligible = set(self.router_descriptions)
        selected: list[str] = []
        for name in skill_names:
            if not isinstance(name, str) or name not in eligible or name in selected:
                continue
            selected.append(name)
            if len(selected) >= MAX_ROUTED_SKILLS:
                break
        return selected


def build_turn_tool_plan(transport: str = "") -> TurnToolPlan:
    """Resolve pure-chat mode, Router eligibility, and initial definitions."""
    from mochi.admin.admin_db import list_tier_assignments
    from mochi.config import (
        TOOL_ESCALATION_ENABLED,
        TOOL_ROUTER_ENABLED,
    )
    from mochi.db import get_skill_mode

    pure_chat = get_skill_mode() == "off"
    if pure_chat:
        resident_names = list(_SKILLOFF_BASE_TOOLS)
        if transport == "telegram":
            resident_names.extend(_SKILLOFF_TELEGRAM_TOOLS)
        resident_definitions = filter_tools(
            skill_registry.get_tools_by_tool_names(
                resident_names,
                transport=transport,
            ),
        )
    else:
        resident_definitions = filter_tools(
            skill_registry.get_tools_by_load("resident", transport=transport),
        )

    if pure_chat:
        return TurnToolPlan(
            pure_chat=True,
            router_enabled=False,
            router_status="pure_chat",
            request_tools_enabled=False,
            resident_definitions=tuple(resident_definitions),
            router_catalog=(),
        )

    request_tools_enabled = bool(TOOL_ESCALATION_ENABLED)
    if request_tools_enabled:
        resident_definitions.append(REQUEST_TOOLS_DEF)

    lite_assigned = bool(list_tier_assignments().get("lite"))
    if not TOOL_ROUTER_ENABLED:
        router_status = "developer_disabled"
    elif not lite_assigned:
        router_status = "lite_unassigned"
    else:
        router_status = "active"

    router_enabled = router_status == "active"
    router_catalog = (
        tuple(build_router_catalog(transport).items())
        if router_enabled
        else ()
    )
    return TurnToolPlan(
        pure_chat=False,
        router_enabled=router_enabled,
        router_status=router_status,
        request_tools_enabled=request_tools_enabled,
        resident_definitions=tuple(resident_definitions),
        router_catalog=router_catalog,
    )


def build_router_catalog(transport: str = "") -> dict[str, str]:
    """Return skills with at least one live, policy-visible routed tool."""
    routed_skills = {
        skill_registry.get_tool_skill(tool["function"]["name"])
        for tool in filter_tools(skill_registry.get_tools(transport))
        if tool.get("_load") == "routed"
    }
    return {
        name: skill.description or name
        for name, skill in skill_registry.all_skills().items()
        if name in routed_skills
    }


def _tool_name(definition: dict) -> str:
    return definition["function"]["name"]


def compose_chat_tools(
    user_id: int,
    resident: list[dict] | tuple[dict, ...],
    routed: list[dict],
    *,
    transport: str = "",
    excluded_skills: frozenset[str] = frozenset(),
) -> list[dict]:
    """Order owner-chat tools as resident, carried session tools, then new ones.

    Tools stay in the same order while the conversation continues, and the
    whole toolbox is revalidated against the live registry every turn.
    """
    from mochi.config import TOOL_SESSION_IDLE_MINUTES, TOOL_SESSION_MAX_EXTRA_TOOLS
    from mochi.db import get_context_reset

    reset_at = get_context_reset(user_id)
    carried_names: tuple[str, ...] = ()
    record = _session_toolboxes.get(user_id)
    if (
        record is not None
        and time.monotonic() - record[0] <= TOOL_SESSION_IDLE_MINUTES * 60
        and record[1] == reset_at
    ):
        carried_names = record[2]

    seen = {_tool_name(tool) for tool in resident}
    carried: list[dict] = []
    for tool in skill_registry.get_tools_by_tool_names(
        carried_names, transport=transport,
    ):
        name = _tool_name(tool)
        if name in seen or skill_registry.get_tool_skill(name) in excluded_skills:
            continue
        seen.add(name)
        carried.append(tool)
    new = [tool for tool in routed if _tool_name(tool) not in seen]
    if len(carried) + len(new) > TOOL_SESSION_MAX_EXTRA_TOOLS:
        resident_names = {_tool_name(tool) for tool in resident}
        carried = []
        new = [tool for tool in routed if _tool_name(tool) not in resident_names]

    extras = [*carried, *new]
    _session_toolboxes[user_id] = (
        time.monotonic(), reset_at, tuple(_tool_name(tool) for tool in extras),
    )
    return [*resident, *extras]


def extend_session_tools(user_id: int, definitions: list[dict]) -> None:
    """Keep tools loaded mid-turn available to the rest of the chat session."""
    record = _session_toolboxes.get(user_id)
    if record is None or not definitions:
        return
    started, reset_at, names = record
    added = tuple(
        name for name in (_tool_name(tool) for tool in definitions)
        if name not in names
    )
    _session_toolboxes[user_id] = (started, reset_at, names + added)
