"""Turn-local request_tools resolution and tool-call budgets."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Iterable

import mochi.skills as skill_registry
from mochi.tool_availability import ToolAvailability
from mochi import tool_policy


MAX_EXACT_REQUESTS = 3
MAX_QUERY_LENGTH = 200
log = logging.getLogger(__name__)
_WORKSPACE_ALIASES = frozenset({
    "mochi_files", "development", "browse_mochi_files", "save_mochi_file",
    "inspect_extension", "write_extension",
})
_WORKSPACE_TOOLS = frozenset({
    "browse_workspace", "edit_workspace", "run_extension", "activate_extension",
})


REQUEST_TOOLS_DEF = {
    "type": "function",
    "function": {
        "name": "request_tools",
        "description": (
            "Load additional tools for later rounds of this turn. "
            "Use skills for exact names: a tool name loads only that tool, "
            "and a skill name loads its requestable tools. "
            "Use query to find at most one matching tool, or combine it with exact names. "
            "Requested tools are not usable in the same response."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skills": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "pattern": r".*\S.*",
                    },
                    "minItems": 1,
                    "maxItems": MAX_EXACT_REQUESTS,
                    "description": "Exact skill names or tool names.",
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_QUERY_LENGTH,
                    "pattern": r".*\S.*",
                    "description": "Natural-language description of the needed capability.",
                },
            },
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class RequestableNamespace:
    name: str
    description: str
    definitions: tuple[dict, ...]
    capability_context: str = ""
    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(_tool_name(definition) for definition in self.definitions)


@dataclass(frozen=True)
class RequestCatalog:
    eligible: dict[str, RequestableNamespace]
    unavailable: dict[str, str]
    tool_to_namespace: dict[str, str]
    tool_loads: dict[str, str]
    resident_tools: dict[str, tuple[str, ...]]
    denied_tools: frozenset[str]
    unavailable_tools: dict[str, str] = field(default_factory=dict)
    search_tools: dict[str, dict] = field(default_factory=dict)


@dataclass
class ToolLoopBudget:
    """Mutable counters owned by one chat() invocation."""

    request_attempts: int = 0
    ordinary_attempts: int = 0
    duplicate_attempts: dict[str, int] = field(default_factory=dict)

    def claim_request(self, limit: int) -> dict | None:
        if self.request_attempts >= limit:
            return error_result(
                "request_limit_reached",
                f"request_tools may be called at most {limit} times per turn.",
            )
        self.request_attempts += 1
        return None

    def claim_tool(
        self,
        tool_name: str,
        arguments: dict,
        *,
        total_limit: int,
        per_tool_limit: int,
    ) -> dict | None:
        fingerprint = tool_name + ":" + json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        current = self.duplicate_attempts.get(fingerprint, 0)
        if per_tool_limit <= 0 or (
            tool_name not in _WORKSPACE_TOOLS and current >= per_tool_limit
        ):
            return {
                "ok": False,
                "code": "repeated_tool_call",
                "started": False,
                "retryable": False,
                "changed": False,
                "message": (
                    "The same tool call is repeating without new arguments. "
                    "Use the previous result or change the approach."
                ),
            }
        if self.ordinary_attempts >= total_limit:
            return {
                "ok": False,
                "code": "tool_call_limit_reached",
                "started": False,
                "retryable": False,
                "changed": False,
                "message": (
                    f"The tool-call limit of {total_limit} was reached."
                ),
            }
        self.ordinary_attempts += 1
        self.duplicate_attempts[fingerprint] = current + 1
        return None


def error_result(code: str, message: str) -> dict:
    return {
        "ok": False,
        "code": code,
        "started": False,
        "retryable": code == "invalid_request",
        "changed": False,
        "message": message,
        "loaded": [],
        "already_loaded": [],
        "matches": [],
        "unavailable": [],
        "no_match": False,
    }


def build_catalog(
    transport: str = "", *, excluded_skills: frozenset[str] = frozenset(),
) -> RequestCatalog:
    """Build the requestable catalog from the live registry and policy."""
    disabled = skill_registry._get_disabled_skills()
    eligible: dict[str, RequestableNamespace] = {}
    unavailable: dict[str, str] = {}
    tool_to_namespace: dict[str, str] = {}
    tool_loads: dict[str, str] = {}
    resident_tools: dict[str, tuple[str, ...]] = {}
    denied_tools: set[str] = set()
    unavailable_tools: dict[str, str] = {}
    search_tools: dict[str, dict] = {}
    from mochi.db import get_adaptive_tool_load_states
    load_states = get_adaptive_tool_load_states()

    for name, skill in skill_registry.all_skills().items():
        if name in excluded_skills:
            unavailable[name] = "not_available_this_turn"
            continue
        definitions = _normalized_definitions(
            skill_registry.get_effective_tools_for_skill(
                name, available=False, states=load_states,
            ),
        )
        for definition in definitions:
            tool_name = _tool_name(definition)
            if tool_name:
                tool_to_namespace[tool_name] = name
                tool_loads[tool_name] = _tool_load(definition)

        reason = _unavailable_reason(
            name,
            skill,
            definitions,
            disabled=disabled,
            transport=transport,
        )
        if reason:
            unavailable[name] = reason
            continue

        available_definitions = _normalized_definitions(
            skill_registry.get_effective_tools_for_skill(name, states=load_states),
        )
        available_names = {_tool_name(item) for item in available_definitions}
        unavailable_tools.update({
            _tool_name(item): "disabled"
            for item in definitions if _tool_name(item) not in available_names
        })
        visible_definitions = _normalized_definitions(
            tool_policy.filter_tools(list(available_definitions)),
        )
        visible_names = {_tool_name(definition) for definition in visible_definitions}
        denied_tools.update(
            _tool_name(definition)
            for definition in available_definitions
            if _tool_name(definition) not in visible_names
        )
        if not visible_definitions:
            unavailable[name] = "policy_denied"
            continue
        for definition in visible_definitions:
            function = definition["function"]
            search_tools[function["name"]] = {
                "skill": name,
                "scope": skill.description,
                "description": function.get("description", ""),
            }

        visible_resident = tuple(
            _tool_name(definition)
            for definition in visible_definitions
            if _tool_load(definition) == "resident"
        )
        if visible_resident:
            resident_tools[name] = visible_resident

        eligible[name] = RequestableNamespace(
            name=name,
            description=getattr(skill, "description", "") or "",
            definitions=visible_definitions,
            capability_context=getattr(skill, "capability_context", "") or "",
        )

    return RequestCatalog(
        eligible=eligible,
        unavailable=unavailable,
        tool_to_namespace=tool_to_namespace,
        tool_loads=tool_loads,
        resident_tools=resident_tools,
        denied_tools=frozenset(denied_tools),
        unavailable_tools=unavailable_tools,
        search_tools=search_tools,
    )


async def _select_tool(query: str, catalog: RequestCatalog) -> str | None:
    if not catalog.search_tools:
        return None
    from mochi.config import TOOL_ROUTER_MAX_TOKENS, TOOL_ROUTER_TIMEOUT_S
    from mochi.db import log_usage
    from mochi.llm import get_client_for_tier, extract_json
    from mochi.prompt_loader import get_prompt
    from mochi.runtime_trace import stage

    template = get_prompt("tool_selector")
    if not template:
        raise RuntimeError("Tool selector prompt is missing")
    tools = [
        {"tool": name, "description": tool["description"], "scope": tool["scope"]}
        for name, tool in sorted(catalog.search_tools.items())
    ]
    prompt = template.replace("{{tool_catalog}}", json.dumps(
        tools, ensure_ascii=False, separators=(",", ":"),
    ))
    client = get_client_for_tier("lite")
    with stage("tool_selection", model_role="LITE", purpose="tool_selection"):
        response = await asyncio.wait_for(asyncio.to_thread(
            client.chat,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": query}],
            tools=None, temperature=0.0, max_tokens=TOOL_ROUTER_MAX_TOKENS,
            json_mode=True, timeout_seconds=TOOL_ROUTER_TIMEOUT_S,
            **({"thinking": False} if client.supports_thinking_control else {}),
        ), timeout=TOOL_ROUTER_TIMEOUT_S)
    log_usage(
        response.prompt_tokens, response.completion_tokens, response.total_tokens,
        model=response.model, purpose="tool_selection", model_role="LITE",
        usage_stage="tool_selection", reasoning_tokens=response.reasoning_tokens,
        cached_prompt_tokens=response.cached_prompt_tokens,
        cache_write_tokens=response.cache_write_tokens,
        model_span_id=response.usage_span_id,
    )
    if response.finish_reason not in {"stop", "end_turn", "completed"}:
        raise ValueError("Tool selection did not complete normally")
    result = json.loads(extract_json(response.content))
    if not isinstance(result, dict) or set(result) != {"tool"}:
        raise ValueError("Tool selection must contain only tool")
    selected = result["tool"]
    if selected is not None and (
        not isinstance(selected, str) or selected not in catalog.search_tools
    ):
        raise ValueError("Tool selection is outside the supplied catalog")
    return selected


async def resolve_request(
    arguments: object,
    availability: ToolAvailability,
    *,
    transport: str = "",
    excluded_skills: frozenset[str] = frozenset(),
) -> tuple[dict, list[dict]]:
    """Resolve one request_tools call and return its result plus new definitions."""
    validation_error = _validate_arguments(arguments)
    if validation_error:
        return error_result("invalid_request", validation_error), []

    args = arguments
    assert isinstance(args, dict)
    catalog = build_catalog(transport, excluded_skills=excluded_skills)
    requested = list(args.get("skills", []))
    query = args.get("query", "").strip()
    query_selected = None
    selection_failed = False
    match_reason = "semantic selection"
    if query:
        exact_names = {
            name.casefold(): name
            for name in (
                *catalog.tool_to_namespace, *catalog.eligible,
                *catalog.unavailable, *catalog.resident_tools, *_WORKSPACE_ALIASES,
            )
        }
        query_selected = exact_names.get(query.casefold())
        if query_selected is not None:
            match_reason = (
                "exact tool name" if query_selected in catalog.tool_to_namespace
                else "exact skill name"
            )
        else:
            try:
                query_selected = await _select_tool(query, catalog)
            except Exception:
                log.exception("Tool semantic selection failed")
                selection_failed = True
            catalog = build_catalog(transport, excluded_skills=excluded_skills)
        if query_selected is not None:
            requested.append(query_selected)

    selected: dict[str, set[str]] = {}
    seen_namespaces: set[str] = set()
    reported_loaded_tools: set[str] = set()
    seen_unavailable: set[tuple[str, str]] = set()
    unavailable_items: list[dict] = []
    already_loaded: list[dict] = []
    renamed: list[dict] = []

    for request in requested:
        exact = request.strip()
        if exact in _WORKSPACE_ALIASES:
            renamed.append({"request": exact, "skill": "personal_workspace"})
            exact = "personal_workspace"
        namespace = (
            exact
            if exact in catalog.eligible
            or exact in catalog.unavailable
            or exact in catalog.resident_tools
            else None
        )
        if namespace is None:
            namespace = catalog.tool_to_namespace.get(exact)
        if namespace is None:
            unavailable_items.append({"request": exact, "reason": "not_found"})
            continue
        if exact in catalog.unavailable_tools:
            unavailable_items.append({
                "request": exact, "reason": catalog.unavailable_tools[exact],
            })
        elif exact in catalog.denied_tools:
            unavailable_items.append({"request": exact, "reason": "policy_denied"})
        elif namespace in catalog.unavailable:
            reason = catalog.unavailable[namespace]
            unavailable_key = (namespace, reason)
            if unavailable_key not in seen_unavailable:
                unavailable_items.append({"request": exact, "reason": reason})
                seen_unavailable.add(unavailable_key)
        elif (
            exact in catalog.tool_loads
            and catalog.tool_loads[exact] == "resident"
            and exact in availability.names
        ):
            if exact not in reported_loaded_tools:
                already_loaded.append({
                    "skill": namespace,
                    "tools": [exact],
                })
                reported_loaded_tools.add(exact)
        elif namespace not in catalog.eligible:
            resident_names = [
                name
                for name in catalog.resident_tools.get(namespace, ())
                if name in availability.names
                and (exact == namespace or name == exact)
            ]
            if resident_names and namespace not in seen_namespaces:
                unreported_names = [
                    name for name in resident_names if name not in reported_loaded_tools
                ]
                if unreported_names:
                    already_loaded.append({
                        "skill": namespace,
                        "tools": unreported_names,
                    })
                    reported_loaded_tools.update(unreported_names)
                seen_namespaces.add(namespace)
            elif not resident_names:
                unavailable_items.append({
                    "request": exact,
                    "reason": "not_found",
                })
        elif namespace in seen_namespaces:
            continue
        else:
            tool_names = catalog.eligible[namespace].tool_names
            if exact == namespace:
                seen_namespaces.add(namespace)
            elif exact in tool_names:
                tool_names = (exact,)
            else:
                unavailable_items.append({"request": exact, "reason": "not_found"})
                continue
            selected.setdefault(namespace, set()).update(tool_names)

    matches = (
        [{"skill": catalog.tool_to_namespace.get(query_selected, query_selected),
          "match_reason": match_reason}]
        if query_selected is not None else []
    )

    loaded: list[dict] = []
    additions: list[dict] = []
    known_names = set(availability.names)
    # A present skill's context was already supplied in system or a loading result.
    present_namespaces = {
        catalog.tool_to_namespace.get(name) for name in availability.names
    }
    for namespace, selected_names in selected.items():
        item = catalog.eligible[namespace]
        definitions = [
            definition for definition in item.definitions
            if _tool_name(definition) in selected_names
        ]
        existing_names = [
            _tool_name(definition) for definition in definitions
            if _tool_name(definition) in availability.names
            and _tool_name(definition) not in reported_loaded_tools
        ]
        if existing_names:
            already_loaded.append({"skill": namespace, "tools": existing_names})
            reported_loaded_tools.update(existing_names)
        new_definitions = [
            definition
            for definition in definitions
            if _tool_name(definition) not in known_names
        ]
        if new_definitions:
            tool_names = [_tool_name(definition) for definition in new_definitions]
            entry = {"skill": namespace, "tools": tool_names}
            if item.capability_context and namespace not in present_namespaces:
                entry["capability_context"] = item.capability_context
            loaded.append(entry)
            additions.extend(new_definitions)
            known_names.update(tool_names)

    result: dict[str, object] = (
        error_result(
            "tool_selection_failed",
            "Tool search did not complete. Exact tool or skill names can still be requested.",
        )
        if selection_failed else {"ok": True}
    )
    if selection_failed:
        result.pop("retryable", None)
    for key, value in (
        ("loaded", loaded),
        ("already_loaded", already_loaded),
        ("matches", matches),
        ("unavailable", unavailable_items),
        ("no_match", bool(query and query_selected is None and not selection_failed)),
        ("renamed", renamed),
    ):
        if value:
            result[key] = value
    return result, additions


def _validate_arguments(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return "arguments must be an object"

    extra = set(arguments) - {"skills", "query"}
    if extra:
        return f"unsupported properties: {', '.join(sorted(extra))}"

    skills = arguments.get("skills")
    query = arguments.get("query")

    if "skills" in arguments:
        if not isinstance(skills, list):
            return "skills must be an array"
        if not skills:
            return "skills must contain at least one item"
        if len(skills) > MAX_EXACT_REQUESTS:
            return f"skills may contain at most {MAX_EXACT_REQUESTS} items"
        if any(not isinstance(item, str) or not item.strip() for item in skills):
            return "skills items must be non-empty strings"
        if any(len(item) > 100 for item in skills):
            return "skills items may contain at most 100 characters"

    if "query" in arguments:
        if not isinstance(query, str):
            return "query must be a string"
        if not query.strip():
            return "query must be non-empty"
        if len(query) > MAX_QUERY_LENGTH:
            return f"query may contain at most {MAX_QUERY_LENGTH} characters"

    has_skills = isinstance(skills, list) and any(item.strip() for item in skills)
    has_query = isinstance(query, str) and bool(query.strip())
    if not has_skills and not has_query:
        return "provide at least one non-empty query or skills item"
    return None


def _unavailable_reason(
    name: str,
    skill: object,
    definitions: tuple[dict, ...],
    *,
    disabled: set[str],
    transport: str,
) -> str | None:
    if name in disabled:
        return "disabled"
    if getattr(skill, "skill_type", "") not in ("tool", "hybrid"):
        return "not_requestable"
    if not definitions:
        return "no_tools"
    if transport and transport in getattr(skill, "exclude_transports", []):
        return "transport_unsupported"

    if skill_registry.get_missing_config(skill):
        return "missing_config"
    return None


def _tool_name(definition: dict) -> str:
    function = definition.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _tool_load(definition: dict) -> str:
    load = definition.get("_load")
    return load if load in {"resident", "routed", "on_demand"} else "on_demand"


def _normalized_definitions(definitions: Iterable[dict]) -> tuple[dict, ...]:
    valid: list[dict] = []
    seen: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        name = _tool_name(definition)
        if not name or name in seen:
            continue
        try:
            json.dumps(definition, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        valid.append(definition)
        seen.add(name)
    return tuple(valid)
