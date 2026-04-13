"""Admin portal — .env read/write utility.

Generalizes the config.py._persist_owner() pattern for safe .env manipulation.
"""

import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Keys that the admin portal is allowed to write via the API.
# Security-sensitive keys (ADMIN_TOKEN) are excluded.
_WRITABLE_KEYS: frozenset[str] = frozenset({
    # LLM — Chat
    "CHAT_PROVIDER", "CHAT_API_KEY", "CHAT_MODEL", "CHAT_BASE_URL",
    # LLM — Think
    "THINK_PROVIDER", "THINK_API_KEY", "THINK_MODEL", "THINK_BASE_URL",
    # Azure
    "AZURE_API_VERSION",
    # Tier routing
    "TIER_LITE_PROVIDER", "TIER_LITE_API_KEY", "TIER_LITE_MODEL", "TIER_LITE_BASE_URL",
    "TIER_CHAT_PROVIDER", "TIER_CHAT_API_KEY", "TIER_CHAT_MODEL", "TIER_CHAT_BASE_URL",
    "TIER_DEEP_PROVIDER", "TIER_DEEP_API_KEY", "TIER_DEEP_MODEL", "TIER_DEEP_BASE_URL",
    # Heartbeat
    "HEARTBEAT_INTERVAL_MINUTES", "AWAKE_HOUR_START", "AWAKE_HOUR_END",
    "SLEEP_KEYWORD_HOUR_START", "SLEEP_KEYWORD_HOUR_END", "SLEEP_KEYWORDS",
    "SILENCE_SLEEP_AFTER_HOUR", "SILENCE_SLEEP_THRESHOLD_HOURS",
    "SILENCE_PAUSE_DAYS", "FALLBACK_WAKE_HOUR",
    "MAX_DAILY_PROACTIVE", "PROACTIVE_COOLDOWN_SECONDS",
    "THINK_FALLBACK_MINUTES", "LLM_HEARTBEAT_TIMEOUT_SECONDS",
    "MAINTENANCE_HOUR", "MAINTENANCE_ENABLED",
    "TIMEZONE_OFFSET_HOURS",
    # Integrations
    "OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "OURA_REFRESH_TOKEN",
    "WEATHER_CITY",
    # Embedding
    "EMBEDDING_PROVIDER", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL",
    "AZURE_EMBEDDING_ENDPOINT", "AZURE_EMBEDDING_API_KEY",
    "AZURE_EMBEDDING_DEPLOYMENT", "AZURE_EMBEDDING_API_VERSION",
    "EMBEDDING_CACHE_MAX_SIZE", "EMBEDDING_CACHE_TTL_S",
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


def _validate_key(key: str) -> None:
    """Reject invalid or disallowed keys."""
    if not re.match(r"^[A-Z][A-Z0-9_]+$", key):
        raise ValueError(f"Invalid env key: {key!r}")
    if key not in _WRITABLE_KEYS:
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

    path = _env_path()
    if path.exists():
        shutil.copy2(path, path.with_suffix(".env.bak"))
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

    path = _env_path()

    if path.exists():
        # Backup before modifying
        shutil.copy2(path, path.with_suffix(".env.bak"))
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


def env_key_is_set(key: str) -> bool:
    """Check if a key has a non-empty value in the current environment."""
    return bool(os.environ.get(key, "").strip())


def read_env_file() -> dict[str, str]:
    """Parse the entire .env file into a dict (keys only, no comments)."""
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
