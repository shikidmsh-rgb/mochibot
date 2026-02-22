"""Runtime state — in-memory state shared across modules.

Not persisted. Resets on restart. Used for cross-module communication
without tight coupling (e.g., heartbeat reads desktop activity status).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from threading import Lock

from mochi.config import TIMEZONE_OFFSET_HOURS

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))
_lock = Lock()


@dataclass
class RuntimeState:
    """Mutable runtime state, thread-safe via lock."""
    # Maintenance results (set nightly, cleared after heartbeat reads them)
    maintenance_summary: str = ""
    # User online/offline status
    user_status: str = "unknown"  # "active" | "idle" | "offline" | "unknown"
    user_status_updated: str = ""
    # Custom state (skills can store arbitrary data here)
    custom: dict = field(default_factory=dict)


_state = RuntimeState()


def get_maintenance_summary() -> str:
    with _lock:
        return _state.maintenance_summary


def set_maintenance_summary(summary: str) -> None:
    with _lock:
        _state.maintenance_summary = summary


def clear_maintenance_summary() -> None:
    with _lock:
        _state.maintenance_summary = ""


def get_user_status() -> str:
    with _lock:
        return _state.user_status


def set_user_status(status: str) -> None:
    with _lock:
        _state.user_status = status
        _state.user_status_updated = datetime.now(TZ).isoformat()


def get_custom(key: str, default=None):
    with _lock:
        return _state.custom.get(key, default)


def set_custom(key: str, value) -> None:
    with _lock:
        _state.custom[key] = value


def clear_custom(key: str) -> None:
    with _lock:
        _state.custom.pop(key, None)
