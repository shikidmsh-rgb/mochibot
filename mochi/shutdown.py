"""Shutdown coordination — request restart from any async context."""

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

RESTART_EXIT_CODE = 42
ADMIN_RESTART_EXIT_CODE = 43
_RESTART_FLAG = Path("data/.restart_requested")

_restart_event: asyncio.Event | None = None


def init_restart_event() -> asyncio.Event:
    """Create the restart event. Called once from main()."""
    global _restart_event
    _restart_event = asyncio.Event()
    return _restart_event


def request_restart(channel_id: int = 0) -> None:
    """Write restart flag and signal the main loop to exit."""
    try:
        _RESTART_FLAG.parent.mkdir(parents=True, exist_ok=True)
        _RESTART_FLAG.write_text(json.dumps({"channel_id": channel_id}))
    except Exception as e:
        log.warning("Failed to write restart flag: %s", e)

    if _restart_event is not None:
        log.info("Restart requested via /restart command")
        _restart_event.set()
    else:
        log.warning("request_restart called before init_restart_event")


def consume_restart_flag() -> int | None:
    """Read and delete the restart flag. Returns channel_id or None."""
    if not _RESTART_FLAG.exists():
        return None
    try:
        data = json.loads(_RESTART_FLAG.read_text())
        _RESTART_FLAG.unlink()
        return data.get("channel_id") or None
    except Exception as e:
        log.warning("Failed to read restart flag: %s", e)
        try:
            _RESTART_FLAG.unlink()
        except OSError:
            pass
        return None
