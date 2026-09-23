"""Observer registry — auto-discovery and management of observers.

Observers are discovered from two locations:
  1. mochi/observers/*/  — standalone observers (infrastructure + legacy)
  2. mochi/skills/*/     — co-located observers (skills with observer: true)

Usage:
    from mochi.observers import discover, collect_all
    discover()                    # scan and load all observers
    data = await collect_all()    # {"weather": {...}, "habit": {...}}

The collect_all() result is merged into the heartbeat observation dict
under the "observers" key.
"""

import importlib
import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Sequence

from mochi.observers.base import (
    CachedObserverView,
    Observer,
    bounded_view_text,
)

log = logging.getLogger(__name__)

_OBSERVERS_DIR = Path(__file__).parent
_SKILLS_DIR = _OBSERVERS_DIR.parent / "skills"

# name → Observer instance
_observers: dict[str, Observer] = {}

OVERVIEW_PAYLOAD_LIMIT = 6000
DETAIL_PAYLOAD_LIMIT = 8000


def _register_observer(obs: Observer, registered: list[str]) -> None:
    """Validate config and register a single observer instance."""
    # Check both os.environ AND DB skill config (admin portal saves to DB)
    from mochi.db import get_skill_config
    db_config = get_skill_config(obs.meta.skill_name or obs.name)
    missing = [
        key
        for key in obs.meta.requires_config
        if not os.getenv(key) and not db_config.get(key)
    ]
    if missing:
        log.info(
            "Observer %s auto-disabled — missing config: %s",
            obs.name, missing,
        )
        obs.meta.enabled = False

    _observers[obs.name] = obs
    registered.append(obs.name)
    log.info(
        "✅ Registered observer: %s (interval=%dm, enabled=%s%s)",
        obs.name, obs.meta.interval, obs.meta.enabled,
        f", skill={obs.meta.skill_name}" if obs.meta.skill_name else "",
    )


def discover() -> list[str]:
    """Scan observers/ and skills/ directories and register all valid observers.

    A valid observer has: observer.py (+ optional OBSERVATION.md).
    Observers whose required config vars are missing are auto-disabled.

    Returns list of registered observer names.
    """
    registered: list[str] = []

    # ── 1. Scan traditional observers/ directory ──
    for entry in sorted(_OBSERVERS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue

        observer_path = entry / "observer.py"
        if not observer_path.exists():
            continue
        if entry.name in _observers:
            continue

        try:
            module = importlib.import_module(
                f"mochi.observers.{entry.name}.observer"
            )
            obs_cls = _find_observer_class(module)
            if obs_cls is None:
                log.warning("No Observer subclass found in observers/%s", entry.name)
                continue

            obs = obs_cls()
            _register_observer(obs, registered)

        except Exception as e:
            log.error(
                "Failed to load observer %s: %s", entry.name, e, exc_info=True
            )

    # ── 2. Scan skills/ for co-located observers ──
    if _SKILLS_DIR.is_dir():
        for entry in sorted(_SKILLS_DIR.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("_"):
                continue

            observer_path = entry / "observer.py"
            if not observer_path.exists():
                continue

            # Skip if already registered from observers/ dir (avoid duplicates)
            if entry.name in _observers:
                continue

            try:
                module = importlib.import_module(
                    f"mochi.skills.{entry.name}.observer"
                )
                obs_cls = _find_observer_class(module)
                if obs_cls is None:
                    log.warning("No Observer subclass found in skills/%s/observer.py", entry.name)
                    continue

                obs = obs_cls()
                _register_observer(obs, registered)

            except Exception as e:
                log.error(
                    "Failed to load co-located observer from skills/%s: %s",
                    entry.name, e, exc_info=True,
                )

    log.info("Observer discovery complete: %d observers available", len(_observers))
    return list(_observers)


def _find_observer_class(module) -> type | None:
    """Find the first Observer subclass in a module."""
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, Observer)
            and attr is not Observer
        ):
            return attr
    return None


async def collect_all() -> dict[str, dict]:
    """Run all enabled observers and return merged result dict.

    Returns: {"weather": {"temp": 22, ...}, "habit": {...}}
    Observers that return {} are omitted from the result.
    Observers linked to a disabled skill (via skill_name) are skipped.
    """
    from mochi.db import get_disabled_skills
    disabled_skills = get_disabled_skills()

    results: dict[str, dict] = {}

    for name, obs in _observers.items():
        if not obs.meta.enabled:
            continue
        if obs._consecutive_errors >= 5:
            continue
        # Skip if owning skill is disabled
        if obs.meta.skill_name and obs.meta.skill_name in disabled_skills:
            continue

        data = await obs.safe_observe()
        if data:
            results[name] = data

    return results


def get_observer(name: str) -> Observer | None:
    """Get an observer by name."""
    return _observers.get(name)


def _unknown_view(source: str, *, detail: bool) -> CachedObserverView:
    return {
        "source": bounded_view_text(source),
        "collected_at": None,
        "available": False,
        "stale": True,
        "error": "unknown_source",
        "failure_count": 0,
        "facts": {},
        "mode": "detail" if detail else "overview",
    }


def _payload_size(views: list[CachedObserverView]) -> int:
    return len(
        json.dumps(
            views,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def _fit_payload_budget(
    views: list[CachedObserverView],
    *,
    limit: int,
) -> list[CachedObserverView]:
    """Keep the batch valid JSON and explicitly mark any budget truncation."""
    fitted = deepcopy(views)
    if _payload_size(fitted) <= limit:
        return fitted

    for view in reversed(fitted):
        if view["facts"]:
            view["facts"] = {"view_truncated": True}
            if _payload_size(fitted) <= limit:
                return fitted

    omitted = 0
    while len(fitted) > 1 and _payload_size(fitted) > limit:
        fitted.pop()
        omitted += 1
    if fitted and omitted:
        fitted[-1]["facts"] = {
            "view_truncated": True,
            "omitted_sources": omitted,
        }
    return fitted


def read_cached_views(
    sources: Sequence[str] | None = None,
    *,
    detail: bool = False,
) -> list[CachedObserverView]:
    """Read safe observer cache projections without collection or side effects."""
    from mochi.db import get_disabled_skills

    requested = list(_observers) if sources is None else list(sources)
    disabled_skills = get_disabled_skills()
    views: list[CachedObserverView] = []

    for source in requested:
        obs = _observers.get(source)
        if obs is None:
            views.append(_unknown_view(str(source), detail=detail))
            continue
        enabled = (
            obs.meta.enabled
            and (
                not obs.meta.skill_name
                or obs.meta.skill_name not in disabled_skills
            )
        )
        views.append(obs.cached_view(enabled=enabled, detail=detail))

    limit = DETAIL_PAYLOAD_LIMIT if detail else OVERVIEW_PAYLOAD_LIMIT
    return _fit_payload_budget(views, limit=limit)
