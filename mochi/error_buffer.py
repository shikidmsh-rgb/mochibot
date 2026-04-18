"""In-memory error buffer for diagnostics.

Captures WARNING+ log records in a bounded ring buffer.
Provides a one-click diagnostic report for bug reporting.
"""

import collections
import logging
import platform
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional

_buffer: collections.deque = collections.deque(maxlen=500)
_log_source: Optional[Callable[[], List[str]]] = None


# ── BufferHandler ───────────────────────────────────────────────────────

class BufferHandler(logging.Handler):
    """Logging handler that captures WARNING+ records into the ring buffer."""

    def __init__(self):
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord):
        try:
            from mochi.config import TZ
            entry = {
                "time": datetime.fromtimestamp(record.created, tz=TZ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
                "traceback": (
                    self.format(record).split("\n", 1)[1]
                    if record.exc_info and record.exc_info[0]
                    else None
                ),
            }
            _buffer.append(entry)
        except Exception:
            pass  # never crash the logging pipeline


# ── Public API ──────────────────────────────────────────────────────────

def get_recent_errors(hours: int = 24) -> list:
    """Return buffer entries from the last *hours*."""
    from mochi.config import TZ
    cutoff = datetime.now(TZ) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    return [e for e in _buffer if e["time"] >= cutoff_str]


def register_log_source(fn: Callable[[], List[str]]):
    """Register a callable that returns recent runtime log lines.

    Called by admin_server to supply bot subprocess stdout without
    error_buffer needing to import admin_server (correct dependency
    direction: admin -> error_buffer).
    """
    global _log_source
    _log_source = fn


# ── Diagnostic report ───────────────────────────────────────────────────

def _mask(val: str) -> str:
    """Mask a potentially sensitive config value."""
    if not val:
        return "(not set)"
    if len(val) > 8:
        return val[:3] + "***" + val[-3:]
    return "***"


def get_diagnostic_report() -> str:
    """Assemble a full plaintext diagnostic report for bug reporting."""
    # Lazy imports to avoid circular dependencies
    from mochi.config import (  # noqa: E402
        CHAT_PROVIDER, CHAT_MODEL, CHAT_BASE_URL, CHAT_API_KEY,
        THINK_PROVIDER, THINK_MODEL,
        TELEGRAM_BOT_TOKEN, WEIXIN_ENABLED,
        EMBEDDING_API_KEY,
        HEARTBEAT_INTERVAL_MINUTES,
        ADMIN_PORT, ADMIN_BIND,
        TIMEZONE_OFFSET_HOURS,
        DB_PATH, TZ,
    )

    lines: list[str] = []
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    # ── Header ──
    lines.append("=" * 50)
    lines.append("  MochiBot Diagnostic Report")
    lines.append("=" * 50)
    lines.append(f"Generated: {now}")
    lines.append("")

    # ── System Info ──
    lines.append("--- System Info ---")
    lines.append(f"Platform: {platform.platform()}")
    lines.append(f"Python: {sys.version}")
    lines.append("")

    # ── Config Summary (safe values shown, secrets masked) ──
    lines.append("--- Config Summary ---")
    lines.append(f"CHAT_PROVIDER: {CHAT_PROVIDER}")
    lines.append(f"CHAT_MODEL: {CHAT_MODEL or '(not set)'}")
    lines.append(f"CHAT_BASE_URL: {CHAT_BASE_URL or '(default)'}")
    lines.append(f"CHAT_API_KEY: {_mask(CHAT_API_KEY)}")
    lines.append(f"THINK_PROVIDER: {THINK_PROVIDER or '(same as chat)'}")
    lines.append(f"THINK_MODEL: {THINK_MODEL or '(same as chat)'}")
    lines.append(f"TELEGRAM_BOT_TOKEN: {_mask(TELEGRAM_BOT_TOKEN)}")
    lines.append(f"WEIXIN_ENABLED: {WEIXIN_ENABLED}")
    lines.append(f"EMBEDDING_API_KEY: {_mask(EMBEDDING_API_KEY)}")
    lines.append(f"HEARTBEAT_INTERVAL_MINUTES: {HEARTBEAT_INTERVAL_MINUTES}")
    lines.append(f"TIMEZONE_OFFSET_HOURS: {TIMEZONE_OFFSET_HOURS}")
    lines.append(f"ADMIN_PORT: {ADMIN_PORT}")
    lines.append(f"ADMIN_BIND: {ADMIN_BIND}")
    lines.append("")

    # ── Database Status ──
    lines.append("--- Database ---")
    db_path = Path(DB_PATH)
    if db_path.exists():
        size = db_path.stat().st_size
        if size > 1024 * 1024:
            size_str = f"{size / 1024 / 1024:.1f} MB"
        else:
            size_str = f"{size / 1024:.1f} KB"
        lines.append(f"Path: {db_path}")
        lines.append(f"Size: {size_str}")
        # integrity check (only in export, not polled)
        try:
            from mochi.db import _connect
            conn = _connect()
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            lines.append(f"Integrity: {result}")
        except Exception as e:
            lines.append(f"Integrity check failed: {e}")
    else:
        lines.append(f"Path: {db_path}")
        lines.append("Status: NOT FOUND")
    lines.append("")

    # ── Recent Errors (24h) ──
    errors = get_recent_errors(24)
    lines.append(f"--- Recent Errors (last 24h): {len(errors)} ---")
    if not errors:
        lines.append("(none)")
    else:
        for e in errors:
            lines.append(
                f"[{e['time']}] {e['level']} {e['name']} — {e['message']}"
            )
            if e.get("traceback"):
                for tb_line in e["traceback"].splitlines():
                    lines.append(f"  {tb_line}")
    lines.append("")

    # ── Recent Run Log ──
    lines.append("--- Recent Run Log ---")
    if _log_source:
        try:
            log_lines = _log_source()
            if log_lines:
                for line in log_lines[-500:]:
                    lines.append(line.rstrip() if isinstance(line, str) else line)
            else:
                lines.append("(no log lines captured)")
        except Exception as e:
            lines.append(f"(failed to read log source: {e})")
    else:
        lines.append("(log source not registered — only available in standalone admin mode)")
    lines.append("")

    lines.append("=" * 50)
    lines.append("  End of Report")
    lines.append("=" * 50)

    return "\n".join(lines)
