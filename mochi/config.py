"""Configuration — loads environment variables with sensible defaults.

All tunables live here. Override via .env file or environment variables.
No hardcoded thresholds/timings elsewhere in the codebase.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def _env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("1", "true", "yes")


# ═══════════════════════════════════════════════════════════════════════════
# LLM — Chat Model (required)
# ═══════════════════════════════════════════════════════════════════════════
# CHAT_PROVIDER tells the framework which SDK to use:
#   "openai"       — OpenAI SDK (also works with DeepSeek, Ollama, Groq, etc.)
#   "azure_openai" — Azure OpenAI SDK
#   "anthropic"    — Anthropic SDK

CHAT_PROVIDER = _env("CHAT_PROVIDER", "openai")
CHAT_API_KEY = _env("CHAT_API_KEY")
CHAT_MODEL = _env("CHAT_MODEL")          # required — no default, user must set
CHAT_BASE_URL = _env("CHAT_BASE_URL")    # optional custom endpoint

# ═══════════════════════════════════════════════════════════════════════════
# LLM — Think Model (optional, falls back to Chat)
# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat Think + nightly maintenance can use a cheaper / faster model.
# Leave empty to reuse the Chat model for everything.

THINK_PROVIDER = _env("THINK_PROVIDER")  # defaults to CHAT_PROVIDER
THINK_API_KEY = _env("THINK_API_KEY")    # defaults to CHAT_API_KEY
THINK_MODEL = _env("THINK_MODEL")        # defaults to CHAT_MODEL
THINK_BASE_URL = _env("THINK_BASE_URL")  # defaults to CHAT_BASE_URL

# ═══════════════════════════════════════════════════════════════════════════
# LLM — Azure-specific (only when provider = azure_openai)
# ═══════════════════════════════════════════════════════════════════════════

AZURE_API_VERSION = _env("AZURE_API_VERSION", "2024-12-01-preview")

# ═══════════════════════════════════════════════════════════════════════════
# Transport
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
DISCORD_BOT_TOKEN = _env("DISCORD_BOT_TOKEN")

# ═══════════════════════════════════════════════════════════════════════════
# Owner
# ═══════════════════════════════════════════════════════════════════════════

OWNER_USER_ID = _env_int("OWNER_USER_ID", 0)


def set_owner_user_id(user_id: int) -> None:
    """Set OWNER_USER_ID at runtime and persist to .env for restart safety."""
    global OWNER_USER_ID
    OWNER_USER_ID = user_id
    # Persist so owner survives restarts (prevents takeover)
    _persist_owner(user_id)


def _persist_owner(user_id: int) -> None:
    """Write OWNER_USER_ID into .env so it survives restarts."""
    env_path = _PROJECT_ROOT / ".env"
    try:
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith("OWNER_USER_ID="):
                    lines[i] = f"OWNER_USER_ID={user_id}"
                    found = True
                    break
            if not found:
                lines.append(f"OWNER_USER_ID={user_id}")
            env_path.write_text("\n".join(lines) + "\n")
        else:
            env_path.write_text(f"OWNER_USER_ID={user_id}\n")
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not persist OWNER_USER_ID to .env — set it manually"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat
# ═══════════════════════════════════════════════════════════════════════════

HEARTBEAT_INTERVAL_MINUTES = _env_int("HEARTBEAT_INTERVAL_MINUTES", 20)
AWAKE_HOUR_START = _env_int("AWAKE_HOUR_START", 7)
AWAKE_HOUR_END = _env_int("AWAKE_HOUR_END", 23)
FORCE_SLEEP_HOUR = _env_int("FORCE_SLEEP_HOUR", 1)
FORCE_WAKE_HOUR = _env_int("FORCE_WAKE_HOUR", 8)
MAX_DAILY_PROACTIVE = _env_int("MAX_DAILY_PROACTIVE", 10)
PROACTIVE_COOLDOWN_SECONDS = _env_int("PROACTIVE_COOLDOWN_SECONDS", 1800)
THINK_FALLBACK_MINUTES = _env_int("THINK_FALLBACK_MINUTES", 60)

# Scheduled daily reports (-1 = disabled, which is the default)
# Enable by setting MORNING_REPORT_HOUR / EVENING_REPORT_HOUR in .env
MORNING_REPORT_HOUR = _env_int("MORNING_REPORT_HOUR", -1)
EVENING_REPORT_HOUR = _env_int("EVENING_REPORT_HOUR", -1)

# ═══════════════════════════════════════════════════════════════════════════
# Memory
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_EXTRACT_INTERVAL_HOURS = _env_int("MEMORY_EXTRACT_INTERVAL_HOURS", 4)
CORE_MEMORY_MAX_TOKENS = _env_int("CORE_MEMORY_MAX_TOKENS", 800)
COMPRESS_DAILY_AFTER_DAYS = _env_int("COMPRESS_DAILY_AFTER_DAYS", 7)
COMPRESS_WEEKLY_AFTER_DAYS = _env_int("COMPRESS_WEEKLY_AFTER_DAYS", 30)
TRASH_PURGE_DAYS = _env_int("TRASH_PURGE_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Maintenance
# ═══════════════════════════════════════════════════════════════════════════

MAINTENANCE_HOUR = _env_int("MAINTENANCE_HOUR", 3)

# ═══════════════════════════════════════════════════════════════════════════
# Optional Integrations
# ═══════════════════════════════════════════════════════════════════════════

TAVILY_API_KEY = _env("TAVILY_API_KEY")

# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

DB_PATH = _PROJECT_ROOT / "data" / "mochi.db"

HEARTBEAT_LOG_TRIM_DAYS = _env_int("HEARTBEAT_LOG_TRIM_DAYS", 7)
HEARTBEAT_LOG_DELETE_DAYS = _env_int("HEARTBEAT_LOG_DELETE_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Timezone (default UTC, override for your locale in .env)
# ═══════════════════════════════════════════════════════════════════════════

TIMEZONE_OFFSET_HOURS = _env_int("TIMEZONE_OFFSET_HOURS", 0)
