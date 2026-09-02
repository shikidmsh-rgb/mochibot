"""Admin portal — .env read/write utility.

Generalizes the config.py._persist_owner() pattern for safe .env manipulation.
"""

import logging
import re
import shutil
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Serialize all .env read-modify-write operations to prevent concurrent
# API requests from clobbering each other's changes.
_ENV_LOCK = threading.Lock()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Keys that the admin portal is allowed to write via the API.
# Security-sensitive keys (ADMIN_TOKEN) are excluded.
_WRITABLE_KEYS: frozenset[str] = frozenset({
    # First-install Main model seed
    "MAIN_PROVIDER", "MAIN_API_KEY", "MAIN_MODEL", "MAIN_BASE_URL",
    # Heartbeat
    "HEARTBEAT_INTERVAL_MINUTES",
    "SILENCE_PAUSE_DAYS", "FALLBACK_WAKE_HOUR",
    "MAX_DAILY_FREE_TIME", "PROACTIVE_COOLDOWN_SECONDS",
    "ATTENTION_INTERVAL_MINUTES", "LLM_HEARTBEAT_TIMEOUT_SECONDS",
    "WAKE_EARLIEST_HOUR", "SLEEP_AFTER_HOUR",
    "FREE_TIME_AWAKE_START", "FREE_TIME_AWAKE_END",
    "MAINTENANCE_HOUR", "MAINTENANCE_ENABLED",
    "WEEKLY_MAINTENANCE_ENABLED", "WEEKLY_MAINTENANCE_MINUTE",
    "TIMEZONE_OFFSET_HOURS",
    "AI_CHAT_MAX_COMPLETION_TOKENS",
    "HEARTBEAT_LOG_TRIM_DAYS", "HEARTBEAT_LOG_DELETE_DAYS",
    "BEDTIME_ENTRY_ENABLED", "BEDTIME_ENTRY_TIMEOUT_S",
    # Integrations
    "WEATHER_CITY",
    # Embedding
    "EMBEDDING_PROVIDER", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL",
    # Transport — Telegram
    "TELEGRAM_BOT_TOKEN",
    # Transport — WeChat
    "WEIXIN_ENABLED", "WEIXIN_BOT_TOKEN", "WEIXIN_BASE_URL",
    "WEIXIN_ALLOWED_USERS",
    # Owner (needed for first-time setup)
    "OWNER_USER_ID",
})


def _env_path() -> Path:
    return _PROJECT_ROOT / ".env"


def _env_bak_path() -> Path:
    return _PROJECT_ROOT / ".env.bak"


def _validate_key(key: str) -> None:
    """Reject invalid or disallowed keys."""
    if not re.match(r"^[A-Z][A-Z0-9_]+$", key):
        raise ValueError(f"Invalid env key: {key!r}")
    # Allow whitelisted keys and SKILL_{NAME}_{KEY} prefixed keys (skill config write-back)
    if key not in _WRITABLE_KEYS and not re.match(r"^SKILL_[A-Z0-9]+_[A-Z0-9_]+$", key):
        raise PermissionError(f"Key {key!r} is not writable via admin portal")


def _validate_value(value: str) -> None:
    """Reject values that could inject .env content."""
    if any(c in value for c in ("\n", "\r", "\0")):
        raise ValueError("Value contains illegal control characters")


def _bootstrap_write_env(key: str, value: str) -> None:
    """Write a key to .env during server bootstrap (bypasses API whitelist).

    This is for server-side startup only (e.g. auto-generating ADMIN_TOKEN).
    The public write_env_value() still enforces the whitelist for API callers.
    """
    _validate_value(value)
    if not re.match(r"^[A-Z][A-Z0-9_]+$", key):
        raise ValueError(f"Invalid env key: {key!r}")

    with _ENV_LOCK:
        path = _env_path()
        if path.exists():
            shutil.copy2(path, _env_bak_path())
            lines = path.read_text(encoding="utf-8").splitlines()
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "=" in stripped and stripped.split("=", 1)[0].strip() == key:
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.write_text(f"{key}={value}\n", encoding="utf-8")

    log.info("Bootstrap: wrote %s to .env", key)


def read_env_value(key: str) -> str | None:
    """Read a key from the .env file directly (not os.environ)."""
    with _ENV_LOCK:
        path = _env_path()
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip("'\"")
        return None


def write_env_value(key: str, value: str) -> None:
    """Write a key=value pair to .env. Creates file if needed.

    - Only whitelisted keys are writable (see _WRITABLE_KEYS).
    - Control characters in values are rejected.
    - Creates a .env.bak backup before writing.
    """
    _validate_key(key)
    _validate_value(value)

    with _ENV_LOCK:
        path = _env_path()

        if path.exists():
            # Backup before modifying
            shutil.copy2(path, _env_bak_path())
            lines = path.read_text(encoding="utf-8").splitlines()
            found = False
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "=" in stripped and stripped.split("=", 1)[0].strip() == key:
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.write_text(f"{key}={value}\n", encoding="utf-8")

    log.info("Wrote %s to .env", key)
def read_env_file() -> dict[str, str]:
    """Parse the entire .env file into a dict (keys only, no comments)."""
    with _ENV_LOCK:
        path = _env_path()
        if not path.exists():
            return {}
        result: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip("'\"")
        return result
