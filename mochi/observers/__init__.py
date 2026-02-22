"""Observer registry — auto-discovery and management of observers.

Observers are discovered by scanning the observers/ directory for subdirectories
containing observer.py and OBSERVATION.md.

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

# name → Observer instance
_observers: dict[str, Observer] = {}


def discover() -> list[str]:
    """Scan the observers/ directory and register all valid observers.

    A valid observer has: observer.py + OBSERVATION.md
    Observers whose required config vars are missing are auto-disabled.

    Returns list of registered observer names.
    """
    registered = []

    for entry in sorted(_OBSERVERS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            continue

        observer_path = entry / "observer.py"
        observation_md = entry / "OBSERVATION.md"

        if not observer_path.exists():
            continue

        try:
            module = importlib.import_module(
                f"mochi.observers.{entry.name}.observer"
            )

            # Find Observer subclass
            obs_cls = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Observer)
                    and attr is not Observer
                ):
                    obs_cls = attr
                    break

            if obs_cls is None:
                log.warning("No Observer subclass found in %s", entry.name)
                continue

            obs = obs_cls()

            # Check required config vars
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
                "✅ Registered observer: %s (interval=%dm, enabled=%s)",
                obs.name, obs.meta.interval, obs.meta.enabled,
            )

        except Exception as e:
            log.error(
                "Failed to load observer %s: %s", entry.name, e, exc_info=True
            )

    log.info("Observer discovery complete: %d observers registered", len(registered))
    return registered


async def collect_all() -> dict[str, dict]:
    """Run all enabled observers and return merged result dict.

    Returns: {"weather": {"temp": 22, ...}, "habit": {...}}
    Observers that return {} are omitted from the result.
    """
    results: dict[str, dict] = {}

    for name, obs in _observers.items():
        if not obs.meta.enabled:
            continue
        if obs._consecutive_errors >= 5:
            continue

        data = await obs.safe_observe()
        if data:
            results[name] = data

    return results


def get_observer(name: str) -> Observer | None:
    """Get an observer by name."""
    return _observers.get(name)


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
