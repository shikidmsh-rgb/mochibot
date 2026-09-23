"""Deterministic opt-in tool loading derived from successful chat use."""

from copy import deepcopy
from datetime import datetime, timedelta

PROMOTION_TURNS = 3
PROMOTION_WINDOW_DAYS = 30
REVERSION_UNUSED_DAYS = 30
MIN_TENURE_DAYS = 7
_LOADS = frozenset({"on_demand", "routed"})


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
    adaptive = bool(definition.get("_adaptive_load")) and declared == "on_demand"
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
    name: str, previous: dict, *, used_turns: int, current: datetime,
) -> dict:
    effective = previous.get("effective_load") or "on_demand"
    pinned = previous.get("pinned_load")
    changed_at = _time(previous.get("changed_at"), current)
    if pinned in _LOADS:
        desired = pinned
        reason = f"pinned by Main to {pinned}"
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
        "declared_load": "on_demand",
        "effective_load": desired,
        "pinned_load": pinned,
        "changed_at": changed_at.isoformat(),
        "used_turns": used_turns,
        "reason": reason,
        "changed": desired != effective,
    }


def _save(state: dict) -> None:
    from mochi.db import save_adaptive_tool_load_state

    save_adaptive_tool_load_state(
        state["tool_name"],
        effective_load=state["effective_load"],
        changed_at=state["changed_at"],
        pinned_load=state["pinned_load"],
        reason=state["reason"],
    )


def recalculate(
    definitions: list[dict], *, user_id: int = 0, now: datetime | None = None,
) -> dict[str, dict]:
    from mochi.db import (
        get_adaptive_tool_load_states, get_successful_chat_tool_turn_counts,
    )

    current = _now(now)
    counts = get_successful_chat_tool_turn_counts(
        user_id=user_id,
        since=(current - timedelta(days=PROMOTION_WINDOW_DAYS)).isoformat(),
    )
    states = get_adaptive_tool_load_states()
    results = {}
    for definition in definitions:
        declared = definition.get("_declared_load") or definition.get("_load")
        name = definition.get("function", {}).get("name")
        if not name or not definition.get("_adaptive_load") or declared != "on_demand":
            continue
        state = _next_state(
            name, states.get(name, {}),
            used_turns=counts.get(name, 0), current=current,
        )
        _save(state)
        results[name] = state
    return results


def pin_definition(
    definition: dict,
    pinned_load: str | None,
    *,
    user_id: int = 0,
    now: datetime | None = None,
) -> dict:
    from mochi.db import (
        get_adaptive_tool_load_states, get_successful_chat_tool_turn_counts,
    )

    if not definition.get("_adaptive_load"):
        raise AdaptiveLoadError("fixed_tool_load")
    name = definition.get("function", {}).get("name")
    declared = definition.get("_declared_load") or definition.get("_load")
    if not name or declared != "on_demand":
        raise AdaptiveLoadError("invalid_adaptive_contract")
    if pinned_load is not None and pinned_load not in _LOADS:
        raise AdaptiveLoadError("invalid_arguments")
    current = _now(now)
    previous = get_adaptive_tool_load_states().get(name, {})
    candidate = dict(previous)
    candidate["pinned_load"] = pinned_load
    if pinned_load is None and previous.get("pinned_load") in _LOADS:
        candidate["effective_load"] = declared
    counts = get_successful_chat_tool_turn_counts(
        user_id=user_id,
        since=(current - timedelta(days=PROMOTION_WINDOW_DAYS)).isoformat(),
    )
    state = _next_state(
        name, candidate, used_turns=counts.get(name, 0), current=current,
    )
    old_load = previous.get("effective_load") or declared
    if state["effective_load"] != old_load:
        state["changed_at"] = current.isoformat()
    state["changed"] = (
        state["effective_load"] != old_load
        or previous.get("pinned_load") != pinned_load
    )
    _save(state)
    return state
