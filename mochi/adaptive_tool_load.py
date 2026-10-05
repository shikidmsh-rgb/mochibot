"""Deterministic opt-in tool loading derived from successful chat use."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from threading import RLock

from mochi.token_estimator import estimate_tokens

PROMOTION_TURNS = 3
PROMOTION_WINDOW_DAYS = 30
REVERSION_UNUSED_DAYS = 30
MIN_TENURE_DAYS = 7
RESIDENT_TURNS = 12
RESIDENT_DAYS = 3
RESIDENT_MAX_TOOLS = 6
RESIDENT_MAX_TOKENS = 3000
_DECLARED_LOADS = frozenset({"on_demand", "routed"})
_LOADS = _DECLARED_LOADS | {"resident"}
_load_lock = RLock()


class AdaptiveLoadError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now(value: datetime | None) -> datetime:
    from mochi.config import TZ

    return (value or datetime.now(TZ)).astimezone(TZ)


def _time(value: object, current: datetime) -> datetime:
    if not value:
        return current
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=current.tzinfo)


def resolve_definition(definition: dict, *, states: dict | None = None) -> dict:
    from mochi.db import get_adaptive_tool_load_states

    resolved = deepcopy(definition)
    declared = str(definition.get("_declared_load") or definition.get("_load") or "on_demand")
    name = str(definition.get("function", {}).get("name") or "")
    adaptive = bool(definition.get("_adaptive_load")) and declared in _DECLARED_LOADS
    if adaptive and states is None:
        states = get_adaptive_tool_load_states()
    state = (states or {}).get(name, {}) if adaptive else {}
    effective = state.get("effective_load", declared)
    if effective not in _LOADS or not adaptive:
        effective = declared
    resolved.update(
        _declared_load=declared,
        _load=effective,
        _adaptive_load=adaptive,
        _load_reason=state.get("reason") or (
            "using declared load" if adaptive else "fixed by skill contract"
        ),
        _load_changed_at=state.get("changed_at"),
        _load_pinned=state.get("pinned_load"),
    )
    return resolved


def _next_state(
    name: str, previous: dict, *, declared: str, used_turns: int,
    used_days: int, current: datetime,
) -> dict:
    effective = previous.get("effective_load") or declared
    pinned = previous.get("pinned_load")
    changed_at = _time(previous.get("changed_at"), current)
    if pinned in _LOADS:
        desired = pinned
        reason = f"pinned by Main to {pinned}"
    elif effective == "resident":
        if current - changed_at >= timedelta(days=MIN_TENURE_DAYS) and used_turns == 0:
            desired = "routed"
            reason = "returned to routed after 30 unused days"
        else:
            desired = effective
            reason = f"kept resident; {used_turns} successful chat turn(s) in 30 days"
    elif used_turns >= RESIDENT_TURNS and used_days >= RESIDENT_DAYS:
        desired = "resident"
        reason = (
            f"promoted to resident after {used_turns} successful chat turn(s) "
            f"across {used_days} day(s) in 30 days"
        )
    elif effective == "routed":
        if current - changed_at >= timedelta(days=MIN_TENURE_DAYS) and used_turns == 0:
            desired = "on_demand"
            reason = "returned to on_demand after 30 unused days"
        else:
            desired = "routed"
            reason = f"kept routed; {used_turns} successful chat turn(s) in 30 days"
    elif used_turns >= PROMOTION_TURNS:
        desired = "routed"
        reason = f"promoted after {used_turns} successful chat turn(s) in 30 days"
    else:
        desired = "on_demand"
        reason = f"using on_demand; {used_turns}/3 successful chat turns"
    if desired != effective:
        changed_at = current
    return {
        "tool_name": name,
        "declared_load": declared,
        "effective_load": desired,
        "pinned_load": pinned,
        "changed_at": changed_at.isoformat(),
        "used_turns": used_turns,
        "used_days": used_days,
        "reason": reason,
        "changed": desired != effective,
    }


def _resident_fits(definitions: list[dict]) -> bool:
    from mochi.skills import get_capability_context_for_tools

    if len(definitions) > RESIDENT_MAX_TOOLS:
        return False
    schemas = [
        {key: value for key, value in definition.items() if not key.startswith("_")}
        for definition in definitions
    ]
    names = [definition["function"]["name"] for definition in definitions]
    text = json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
    text += get_capability_context_for_tools(names)
    return estimate_tokens(text) <= RESIDENT_MAX_TOKENS


def _bound_residents(
    results: dict[str, dict], definitions: dict[str, dict],
    previous: dict[str, dict], current: datetime,
) -> None:
    candidates = sorted(
        (state for state in results.values() if state["effective_load"] == "resident"),
        key=lambda state: (
            state["pinned_load"] != "resident",
            previous.get(state["tool_name"], {}).get("effective_load") != "resident",
            -state["used_days"], -state["used_turns"], state["tool_name"],
        ),
    )
    selected = []
    for state in candidates:
        name = state["tool_name"]
        candidate = [*selected, definitions[name]]
        if _resident_fits(candidate):
            selected = candidate
            continue
        if state["pinned_load"] == "resident":
            raise AdaptiveLoadError("resident_budget_exceeded")
        old = previous.get(name, {}).get("effective_load") or state["declared_load"]
        state.update(
            effective_load="routed",
            reason="resident budget full; kept routed",
            changed=old != "routed",
            changed_at=(
                current.isoformat() if old != "routed"
                else previous.get(name, {}).get("changed_at", current.isoformat())
            ),
        )


def recalculate(
    definitions: list[dict], *, user_id: int = 0, now: datetime | None = None,
) -> dict[str, dict]:
    from mochi.db import (
        get_adaptive_tool_load_states, get_successful_chat_tool_usage,
        save_adaptive_tool_load_states,
    )

    current = _now(now)
    with _load_lock:
        usage = get_successful_chat_tool_usage(
            user_id=user_id,
            since=(current - timedelta(days=PROMOTION_WINDOW_DAYS)).isoformat(),
        )
        states = get_adaptive_tool_load_states()
        results = {}
        eligible = {}
        for definition in definitions:
            declared = definition.get("_declared_load") or definition.get("_load")
            name = definition.get("function", {}).get("name")
            if (
                not name or not definition.get("_adaptive_load")
                or declared not in _DECLARED_LOADS
            ):
                continue
            counts = usage.get(name, {})
            results[name] = _next_state(
                name, states.get(name, {}), declared=declared,
                used_turns=counts.get("turns", 0),
                used_days=counts.get("days", 0), current=current,
            )
            eligible[name] = definition
        _bound_residents(results, eligible, states, current)
        save_adaptive_tool_load_states(list(results.values()))
        return results


def pin_definition(
    definition: dict,
    pinned_load: str | None,
    *,
    user_id: int = 0,
    now: datetime | None = None,
) -> dict:
    from mochi.db import (
        get_adaptive_tool_load_states, get_successful_chat_tool_usage,
        save_adaptive_tool_load_states,
    )
    from mochi.skills import get_declared_tools

    if not definition.get("_adaptive_load"):
        raise AdaptiveLoadError("fixed_tool_load")
    name = definition.get("function", {}).get("name")
    declared = definition.get("_declared_load") or definition.get("_load")
    if not name or declared not in _DECLARED_LOADS:
        raise AdaptiveLoadError("invalid_adaptive_contract")
    if pinned_load is not None and pinned_load not in _LOADS:
        raise AdaptiveLoadError("invalid_arguments")
    current = _now(now)
    with _load_lock:
        states = get_adaptive_tool_load_states()
        previous = states.get(name, {})
        candidate = {**previous, "pinned_load": pinned_load}
        if pinned_load is None and previous.get("pinned_load") in _LOADS:
            candidate["effective_load"] = declared
        usage = get_successful_chat_tool_usage(
            user_id=user_id,
            since=(current - timedelta(days=PROMOTION_WINDOW_DAYS)).isoformat(),
        ).get(name, {})
        state = _next_state(
            name, candidate, declared=declared,
            used_turns=usage.get("turns", 0), used_days=usage.get("days", 0),
            current=current,
        )
        if state["effective_load"] == "resident":
            other_residents = [
                tool for tool in get_declared_tools()
                if tool["function"]["name"] != name and tool.get("_adaptive_load")
                and states.get(tool["function"]["name"], {}).get("effective_load") == "resident"
            ]
            if not _resident_fits([*other_residents, definition]):
                if pinned_load == "resident":
                    raise AdaptiveLoadError("resident_budget_exceeded")
                state.update(
                    effective_load="routed", reason="resident budget full; kept routed",
                )
        old_load = previous.get("effective_load") or declared
        if state["effective_load"] != old_load:
            state["changed_at"] = current.isoformat()
        else:
            state["changed_at"] = _time(previous.get("changed_at"), current).isoformat()
        state["changed"] = (
            state["effective_load"] != old_load
            or previous.get("pinned_load") != pinned_load
        )
        save_adaptive_tool_load_states([state])
        return state
