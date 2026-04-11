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
import logging
import os
from pathlib import Path

from mochi.observers.base import Observer

log = logging.getLogger(__name__)

_OBSERVERS_DIR = Path(__file__).parent
_SKILLS_DIR = _OBSERVERS_DIR.parent / "skills"

# name → Observer instance
_observers: dict[str, Observer] = {}


def _register_observer(obs: Observer, registered: list[str]) -> None:
    """Validate config and register a single observer instance."""
    missing = [
        key
        for key in obs.meta.requires_config
        if not os.getenv(key)
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

    log.info("Observer discovery complete: %d observers registered", len(registered))
    return registered


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


def get_all_observers() -> dict[str, Observer]:
    """Get all registered observers. Used by heartbeat for delta detection."""
    return _observers


def list_observers() -> list[dict]:
    """List all registered observers with metadata."""
    return [
        {
            "name": obs.name,
            "interval": obs.meta.interval,
            "enabled": obs.meta.enabled,
            "consecutive_errors": obs._consecutive_errors,
            "last_collected_at": (
                obs._last_collected_at.isoformat()
                if obs._last_collected_at
                else None
            ),
        }
        for obs in _observers.values()
    ]


def get_observer_info_all() -> list[dict]:
    """Return metadata for all registered observers (for admin display).

    Only returns standalone observers (no skill_name) that have non-empty
    requires_config.  Co-located observers are already represented by their
    owning skill in get_skill_info_all().
    """
    result = []
    for obs in _observers.values():
        if not obs.meta.requires_config:
            continue
        # Skip co-located — the skill already covers config UI
        if obs.meta.skill_name:
            continue
        result.append({
            "name": obs.name,
            "description": f"Observer: {obs.name} (interval {obs.meta.interval}min)",
            "type": "observer",
            "expose_as_tool": False,
            "multi_turn": False,
            "triggers": [],
            "tools": [],
            "has_usage_rules": False,
            "requires_config": obs.meta.requires_config,
            "enabled": obs.meta.enabled,
            "config_status": {
                key: bool(os.getenv(key))
                for key in obs.meta.requires_config
            },
        })
    return result


def get_observers_for_admin() -> list[dict]:
    """Return all observers with full metadata for Heartbeat admin page.

    Unlike get_observer_info_all() which filters, this returns ALL observers
    for display in the Heartbeat → Observers section.
    """
    from mochi.db import get_disabled_skills
    disabled = get_disabled_skills()

    result = []
    for obs in _observers.values():
        is_linked = bool(obs.meta.skill_name)
        is_disabled = (
            not obs.meta.enabled
            or (is_linked and obs.meta.skill_name in disabled)
        )
        result.append({
            "name": obs.name,
            "default_interval": obs.meta.interval,
            "effective_interval": obs.effective_interval,
            "has_override": obs.effective_interval != obs.meta.interval,
            "enabled": not is_disabled,
            "skill_name": obs.meta.skill_name or None,
            "infrastructure": not is_linked and not obs.meta.requires_config,
            "consecutive_errors": obs._consecutive_errors,
            "last_collected_at": (
                obs._last_collected_at.isoformat()
                if obs._last_collected_at
                else None
            ),
        })
    return result
