"""Compute the tool policy for one chat turn."""

from __future__ import annotations

from dataclasses import dataclass

import mochi.skills as skill_registry
from mochi.request_tools import REQUEST_TOOLS_DEF
from mochi.tool_policy import filter_tools


_SKILLOFF_BASE_TOOLS = ("update_core", "manage_agent_settings")
_SKILLOFF_TELEGRAM_TOOLS = ("send_sticker",)
MAX_ROUTED_SKILLS = 2


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
    catalog: dict[str, str] = {}
    for name, skill in skill_registry.all_skills().items():
        definitions = filter_tools(
            skill_registry.get_tools_by_names(
                [name],
                transport=transport,
                loads={"routed"},
            ),
        )
        if not definitions:
            continue
        catalog[name] = skill.description or name
    return catalog
