"""Turn-local request_tools resolution and tool-call budgets."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

import mochi.skills as skill_registry
from mochi.tool_availability import ToolAvailability
from mochi import tool_policy


MAX_EXACT_REQUESTS = 3
MAX_QUERY_LENGTH = 200
MAX_REASON_LENGTH = 120
MAX_SEARCH_MATCHES = 2
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
            "Load additional tool namespaces for later rounds of this turn. "
            "Use skills for exact skill/tool names, query for a short natural-language "
            "search, or both. Requested tools are not usable in the same response."
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
                "reason": {
                    "type": "string",
                    "maxLength": MAX_REASON_LENGTH,
                    "description": "Brief reason the capability is needed.",
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

        visible_resident = tuple(
            _tool_name(definition)
            for definition in visible_definitions
            if _tool_load(definition) == "resident"
        )
        if visible_resident:
            resident_tools[name] = visible_resident

        requestable_definitions = tuple(
            definition
            for definition in visible_definitions
            if _tool_load(definition) != "resident" or skill.external
        )
        if not requestable_definitions:
            continue

        eligible[name] = RequestableNamespace(
            name=name,
            description=getattr(skill, "description", "") or "",
            definitions=requestable_definitions,
        )

    return RequestCatalog(
        eligible=eligible,
        unavailable=unavailable,
        tool_to_namespace=tool_to_namespace,
        tool_loads=tool_loads,
        resident_tools=resident_tools,
        denied_tools=frozenset(denied_tools),
        unavailable_tools=unavailable_tools,
    )


def resolve_request(
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
    requested = args.get("skills", [])
    query = args.get("query", "").strip()

    selected: list[str] = []
    seen_namespaces: set[str] = set()
    seen_resident_tools: set[str] = set()
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
            if exact not in seen_resident_tools:
                already_loaded.append({
                    "skill": namespace,
                    "tools": [exact],
                })
                seen_resident_tools.add(exact)
        elif namespace not in catalog.eligible:
            resident_names = [
                name
                for name in catalog.resident_tools.get(namespace, ())
                if name in availability.names
            ]
            if resident_names and namespace not in seen_namespaces:
                already_loaded.append({
                    "skill": namespace,
                    "tools": resident_names,
                })
                seen_namespaces.add(namespace)
            elif not resident_names:
                unavailable_items.append({
                    "request": exact,
                    "reason": "not_found",
                })
        elif namespace in seen_namespaces:
            continue
        else:
            seen_namespaces.add(namespace)
            selected.append(namespace)

    matches: list[dict] = []
    query_matched_catalog = False
    if query:
        query_matched_catalog = any(
            _match_score(query, item)[0]
            for item in catalog.eligible.values()
        )
        for namespace, match_reason in _search_catalog(
            query,
            catalog.eligible.values(),
            excluded=seen_namespaces,
        ):
            seen_namespaces.add(namespace)
            selected.append(namespace)
            matches.append({"skill": namespace, "match_reason": match_reason})

    loaded: list[dict] = []
    additions: list[dict] = []
    known_names = set(availability.names)
    for namespace in selected:
        item = catalog.eligible[namespace]
        new_definitions = [
            definition
            for definition in item.definitions
            if _tool_name(definition) not in known_names
        ]
        if new_definitions:
            tool_names = [_tool_name(definition) for definition in new_definitions]
            loaded.append({"skill": namespace, "tools": tool_names})
            additions.extend(new_definitions)
            known_names.update(tool_names)
        else:
            already_loaded.append({
                "skill": namespace,
                "tools": list(item.tool_names),
            })

    return {
        "ok": True,
        "loaded": loaded,
        "already_loaded": already_loaded,
        "matches": matches,
        "unavailable": unavailable_items,
        "no_match": bool(query and not query_matched_catalog),
        **({"renamed": renamed} if renamed else {}),
    }, additions


def _validate_arguments(arguments: object) -> str | None:
    if not isinstance(arguments, dict):
        return "arguments must be an object"

    extra = set(arguments) - {"skills", "query", "reason"}
    if extra:
        return f"unsupported properties: {', '.join(sorted(extra))}"

    skills = arguments.get("skills")
    query = arguments.get("query")
    reason = arguments.get("reason")

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

    if "reason" in arguments:
        if not isinstance(reason, str):
            return "reason must be a string"
        if len(reason) > MAX_REASON_LENGTH:
            return f"reason may contain at most {MAX_REASON_LENGTH} characters"

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


def _search_catalog(
    query: str,
    namespaces: Iterable[RequestableNamespace],
    *,
    excluded: set[str],
) -> list[tuple[str, str]]:
    ranked: list[tuple[int, str, str]] = []
    for item in namespaces:
        if item.name in excluded:
            continue
        score, reason = _match_score(query, item)
        if score:
            ranked.append((score, item.name, reason))
    ranked.sort(key=lambda match: (-match[0], match[1]))
    if ranked:
        minimum_score = ranked[0][0] - 10
        ranked = [match for match in ranked if match[0] >= minimum_score]
    return [
        (namespace, reason)
        for _, namespace, reason in ranked[:MAX_SEARCH_MATCHES]
    ]


def _match_score(query: str, item: RequestableNamespace) -> tuple[int, str]:
    normalized = query.casefold().strip()
    name = item.name.casefold()
    tool_names = [tool.casefold() for tool in item.tool_names]
    description = item.description.casefold()

    if normalized == name:
        return 100, "exact skill name"
    if normalized in tool_names:
        return 95, "exact tool name"
    if normalized in name:
        return 80, "skill name contains query"
    if any(normalized in tool for tool in tool_names):
        return 75, "tool name contains query"
    if normalized in description:
        return 70, "description contains query"

    cjk_match_length = _longest_cjk_match(normalized, " ".join([
        name,
        *tool_names,
        description,
    ]))
    if cjk_match_length >= 2:
        return 40 + (10 * cjk_match_length), "CJK phrase match"

    tokens = [
        token
        for token in re.findall(r"[\w]+", normalized, flags=re.UNICODE)
        if token
    ]
    if not tokens:
        return 0, ""
    haystack = " ".join([name, *tool_names, description])
    matched = sum(token in haystack for token in tokens)
    if not matched:
        return 0, ""
    return 10 * matched + (5 if matched == len(tokens) else 0), "metadata token match"


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


def _longest_cjk_match(left: str, right: str) -> int:
    left_runs = re.findall(r"[\u3400-\u9fff]+", left)
    right_runs = re.findall(r"[\u3400-\u9fff]+", right)
    longest = 0
    for left_run in left_runs:
        for right_run in right_runs:
            shorter, longer = sorted((left_run, right_run), key=len)
            for length in range(min(len(shorter), 8), longest, -1):
                if any(
                    shorter[start:start + length] in longer
                    for start in range(len(shorter) - length + 1)
                ):
                    longest = length
                    break
    return longest
