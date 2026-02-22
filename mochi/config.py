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

def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


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
# Model Tier Routing (3-tier system: lite / chat / deep)
# ═══════════════════════════════════════════════════════════════════════════
# When TIER_ROUTING_ENABLED=true, each tier can use a different model/provider.
# When false (default), all tiers fall back to CHAT_* / THINK_* config.
# Zero-config = existing 2-model behavior.

TIER_ROUTING_ENABLED = _env_bool("TIER_ROUTING_ENABLED", False)

TIER_LITE_PROVIDER = _env("TIER_LITE_PROVIDER")
TIER_LITE_API_KEY = _env("TIER_LITE_API_KEY")
TIER_LITE_MODEL = _env("TIER_LITE_MODEL")
TIER_LITE_BASE_URL = _env("TIER_LITE_BASE_URL")

TIER_CHAT_PROVIDER = _env("TIER_CHAT_PROVIDER")
TIER_CHAT_API_KEY = _env("TIER_CHAT_API_KEY")
TIER_CHAT_MODEL = _env("TIER_CHAT_MODEL")
TIER_CHAT_BASE_URL = _env("TIER_CHAT_BASE_URL")

TIER_DEEP_PROVIDER = _env("TIER_DEEP_PROVIDER")
TIER_DEEP_API_KEY = _env("TIER_DEEP_API_KEY")
TIER_DEEP_MODEL = _env("TIER_DEEP_MODEL")
TIER_DEEP_BASE_URL = _env("TIER_DEEP_BASE_URL")

# ═══════════════════════════════════════════════════════════════════════════
# Embedding (vector memory search)
# ═══════════════════════════════════════════════════════════════════════════

# Provider-agnostic embedding config
EMBEDDING_PROVIDER = _env("EMBEDDING_PROVIDER")            # openai | azure_openai | ollama | none
EMBEDDING_API_KEY = _env("EMBEDDING_API_KEY")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL")
EMBEDDING_BASE_URL = _env("EMBEDDING_BASE_URL")

# Legacy Azure embedding vars (backward-compat fallback)
AZURE_EMBEDDING_ENDPOINT = _env("AZURE_EMBEDDING_ENDPOINT")
AZURE_EMBEDDING_API_KEY = _env("AZURE_EMBEDDING_API_KEY")
AZURE_EMBEDDING_DEPLOYMENT = _env("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
AZURE_EMBEDDING_API_VERSION = _env("AZURE_EMBEDDING_API_VERSION", "2024-10-21")
EMBEDDING_CACHE_MAX_SIZE = _env_int("EMBEDDING_CACHE_MAX_SIZE", 128)
EMBEDDING_CACHE_TTL_S = _env_int("EMBEDDING_CACHE_TTL_S", 300)

# ═══════════════════════════════════════════════════════════════════════════
# Transport
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")

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
AWAKE_HOUR_START = _env_int("AWAKE_HOUR_START", 7)      # startup-only: init state
AWAKE_HOUR_END = _env_int("AWAKE_HOUR_END", 23)         # startup-only: init state
MAX_DAILY_PROACTIVE = _env_int("MAX_DAILY_PROACTIVE", 10)
PROACTIVE_COOLDOWN_SECONDS = _env_int("PROACTIVE_COOLDOWN_SECONDS", 1800)
THINK_FALLBACK_MINUTES = _env_int("THINK_FALLBACK_MINUTES", 60)
LLM_HEARTBEAT_TIMEOUT_SECONDS = _env_int("LLM_HEARTBEAT_TIMEOUT_SECONDS", 120)
PROACTIVE_CHAT_MAX_TOKENS = _env_int("PROACTIVE_CHAT_MAX_TOKENS", 512)
PROACTIVE_CHAT_HISTORY_TURNS = _env_int("PROACTIVE_CHAT_HISTORY_TURNS", 10)

# Sleep/Wake State Machine
SLEEP_KEYWORD_HOUR_START = _env_int("SLEEP_KEYWORD_HOUR_START", 21)
SLEEP_KEYWORD_HOUR_END = _env_int("SLEEP_KEYWORD_HOUR_END", 4)
SLEEP_KEYWORDS = _env("SLEEP_KEYWORDS", "晚安,睡了,去睡了,good night,gn").split(",")
SILENCE_SLEEP_AFTER_HOUR = _env_int("SILENCE_SLEEP_AFTER_HOUR", 23)
SILENCE_SLEEP_THRESHOLD_HOURS = _env_float("SILENCE_SLEEP_THRESHOLD_HOURS", 1.0)
SILENCE_PAUSE_DAYS = _env_float("SILENCE_PAUSE_DAYS", 3.0)
FALLBACK_WAKE_HOUR = _env_int("FALLBACK_WAKE_HOUR", 10)

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
MAINTENANCE_ENABLED = _env_bool("MAINTENANCE_ENABLED", True)

# ═══════════════════════════════════════════════════════════════════════════
# Diary
# ═══════════════════════════════════════════════════════════════════════════

DIARY_STATUS_MAX_LINES = _env_int("DIARY_STATUS_MAX_LINES", 20)
DIARY_ENTRY_MAX_LINES = _env_int("DIARY_ENTRY_MAX_LINES", 50)

# ═══════════════════════════════════════════════════════════════════════════
# Optional Integrations
# ═══════════════════════════════════════════════════════════════════════════

WEB_SEARCH_TIMEOUT_S = _env_int("WEB_SEARCH_TIMEOUT_S", 20)

# ═══════════════════════════════════════════════════════════════════════════
# Oura Ring (optional — OAuth2)
# ═════════════════════════════════════════════════════════════════════════
# Run `python oura_auth.py` to authorize and get your tokens.

OURA_CLIENT_ID = _env("OURA_CLIENT_ID")
OURA_CLIENT_SECRET = _env("OURA_CLIENT_SECRET")
OURA_REFRESH_TOKEN = _env("OURA_REFRESH_TOKEN")

# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

DB_PATH = _PROJECT_ROOT / "data" / "mochi.db"

HEARTBEAT_LOG_TRIM_DAYS = _env_int("HEARTBEAT_LOG_TRIM_DAYS", 7)
HEARTBEAT_LOG_DELETE_DAYS = _env_int("HEARTBEAT_LOG_DELETE_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Timezone (default UTC, override for your locale in .env)
# ═══════════════════════════════════════════════════════════════════════════

TIMEZONE_OFFSET_HOURS = _env_int("TIMEZONE_OFFSET_HOURS", 8)

from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))


def logical_today(now: datetime | None = None) -> str:
    """Return today's date as YYYY-MM-DD, rolling over at MAINTENANCE_HOUR.

    Before MAINTENANCE_HOUR, entries belong to "yesterday" (the previous
    logical day). This keeps nightly archival and day boundaries consistent.
    """
    if now is None:
        now = datetime.now(TZ)
    if now.hour < MAINTENANCE_HOUR:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def logical_yesterday(now: datetime | None = None) -> str:
    """Return yesterday's logical date as YYYY-MM-DD."""
    if now is None:
        now = datetime.now(TZ)
    today = datetime.strptime(logical_today(now), "%Y-%m-%d")
    return (today - timedelta(days=1)).strftime("%Y-%m-%d")

# ═══════════════════════════════════════════════════════════════════════════
# Memory Recall / Vector Search
# ═══════════════════════════════════════════════════════════════════════════

RECALL_VEC_SIM_THRESHOLD = _env_float("RECALL_VEC_SIM_THRESHOLD", 0.25)
RECALL_BM25_WEIGHT = _env_float("RECALL_BM25_WEIGHT", 2.0)
RECALL_VEC_SIM_WEIGHT = _env_float("RECALL_VEC_SIM_WEIGHT", 6.0)
RECALL_KEYWORD_BOOST = _env_float("RECALL_KEYWORD_BOOST", 1.0)
RECALL_FTS_CANDIDATE_MULTIPLIER = _env_int("RECALL_FTS_CANDIDATE_MULTIPLIER", 5)
RECALL_FALLBACK_LIMIT = _env_int("RECALL_FALLBACK_LIMIT", 100)
VEC_SEARCH_NATIVE_ENABLED = _env_bool("VEC_SEARCH_NATIVE_ENABLED", True)
VEC_EMBEDDING_DIM = _env_int("VEC_EMBEDDING_DIM", 1536)
VEC_SEARCH_CANDIDATE_LIMIT = _env_int("VEC_SEARCH_CANDIDATE_LIMIT", 50)

# ═══════════════════════════════════════════════════════════════════════════
# Memory Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_DEMOTE_AFTER_DAYS = _env_int("MEMORY_DEMOTE_AFTER_DAYS", 60)
MEMORY_DEMOTE_MIN_ACCESS = _env_int("MEMORY_DEMOTE_MIN_ACCESS", 3)
RECALL_DECAY_HALF_LIFE_DAYS = _env_float("RECALL_DECAY_HALF_LIFE_DAYS", 30.0)

# ═══════════════════════════════════════════════════════════════════════════
# Token Limits
# ═══════════════════════════════════════════════════════════════════════════

AI_CHAT_MAX_COMPLETION_TOKENS = _env_int("AI_CHAT_MAX_COMPLETION_TOKENS", 4096)
TOOL_LOOP_MAX_ROUNDS = _env_int("TOOL_LOOP_MAX_ROUNDS", 5)
TOOL_LOOP_PER_TOOL_LIMIT = _env_int("TOOL_LOOP_PER_TOOL_LIMIT", 5)

# ═══════════════════════════════════════════════════════════════════════════
# Observer Thresholds
# ═══════════════════════════════════════════════════════════════════════════

DELTA_SILENCE_JUMP_HOURS = _env_float("DELTA_SILENCE_JUMP_HOURS", 1.5)
DELTA_NEW_TODOS = _env_int("DELTA_NEW_TODOS", 3)
OBSERVER_FAILURE_ALERT_THRESHOLD = _env_int("OBSERVER_FAILURE_ALERT_THRESHOLD", 3)

# ═══════════════════════════════════════════════════════════════════════════
# Log Compression
# ═══════════════════════════════════════════════════════════════════════════

PET_LOG_WEEKLY_AFTER_DAYS = _env_int("PET_LOG_WEEKLY_AFTER_DAYS", 7)
PET_LOG_MONTHLY_AFTER_DAYS = _env_int("PET_LOG_MONTHLY_AFTER_DAYS", 30)
LIFE_LOG_WEEKLY_AFTER_DAYS = _env_int("LIFE_LOG_WEEKLY_AFTER_DAYS", 7)
LIFE_LOG_MONTHLY_AFTER_DAYS = _env_int("LIFE_LOG_MONTHLY_AFTER_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Router
# ═══════════════════════════════════════════════════════════════════════════

TOOL_ROUTER_ENABLED = _env_bool("TOOL_ROUTER_ENABLED", False)
TOOL_ROUTER_MAX_TOKENS = _env_int("TOOL_ROUTER_MAX_TOKENS", 100)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Escalation
# ═══════════════════════════════════════════════════════════════════════════

TOOL_ESCALATION_ENABLED = _env_bool("TOOL_ESCALATION_ENABLED", True)
TOOL_ESCALATION_MAX_PER_TURN = _env_int("TOOL_ESCALATION_MAX_PER_TURN", 2)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Governance
# ═══════════════════════════════════════════════════════════════════════════

TOOL_DENY_NAMES = _env("TOOL_DENY_NAMES", "")           # comma-separated denylist
TOOL_REQUIRE_CONFIRM = _env("TOOL_REQUIRE_CONFIRM", "")  # comma-separated, needs user confirmation
TOOL_RATE_LIMIT_PER_MIN = _env_int("TOOL_RATE_LIMIT_PER_MIN", 10)

# ═══════════════════════════════════════════════════════════════════════════
# Chatty Rhythm
# ═══════════════════════════════════════════════════════════════════════════

TG_INTERIM_ENABLED = _env_bool("TG_INTERIM_ENABLED", True)
TG_BUBBLE_DELAY_S = _env_float("TG_BUBBLE_DELAY_S", 1.0)
TG_BUBBLE_MAX = _env_int("TG_BUBBLE_MAX", 4)
TG_BUBBLE_DELIMITER = _env("TG_BUBBLE_DELIMITER", "|||")
TG_BUBBLE_MIN_CHARS = _env_int("TG_BUBBLE_MIN_CHARS", 8)
TG_AGGREGATE_ENABLED = _env_bool("TG_AGGREGATE_ENABLED", True)


# ═══════════════════════════════════════════════════════════════════════════
# Admin Portal
# ═══════════════════════════════════════════════════════════════════════════

ADMIN_ENABLED = _env_bool("ADMIN_ENABLED", True)
ADMIN_PORT = _env_int("ADMIN_PORT", 8080)
ADMIN_BIND = _env("ADMIN_BIND", "127.0.0.1")   # default localhost-only; set 0.0.0.0 for remote access
ADMIN_TOKEN = _env("ADMIN_TOKEN")               # optional; if set, all requests must include this token


# ═══════════════════════════════════════════════════════════════════════════
# Startup Validation
# ═══════════════════════════════════════════════════════════════════════════

def validate_config() -> None:
    """Preflight check at startup — exit on missing critical config."""
    import sys
    import logging as _logging
    _log = _logging.getLogger(__name__)
    issues: list[tuple[str, str, str]] = []

    if not CHAT_MODEL:
        issues.append(("CRITICAL", "CHAT_MODEL", "No LLM model configured"))
    if not CHAT_API_KEY and CHAT_PROVIDER != "ollama":
        issues.append(("CRITICAL", "CHAT_API_KEY", "No API key for chat model"))
    if not TELEGRAM_BOT_TOKEN:
        issues.append(("WARN", "TELEGRAM_BOT_TOKEN",
                        "No transport configured — bot will not receive messages"))

    has_critical = False
    for level, name, impact in issues:
        if level == "CRITICAL":
            _log.critical("[%s] %s — %s", level, name, impact)
            has_critical = True
        else:
            _log.warning("[%s] %s — %s", level, name, impact)

    if has_critical:
        _log.critical("Critical config missing. Set them in .env and restart.")
        sys.exit(1)

    # Deprecation warnings for removed config keys
    for old_key, new_key in [
        ("FORCE_SLEEP_HOUR", "SILENCE_SLEEP_AFTER_HOUR"),
        ("FORCE_WAKE_HOUR", "FALLBACK_WAKE_HOUR"),
    ]:
        if os.getenv(old_key):
            _log.warning(
                "[DEPRECATED] %s is no longer used. Use %s instead. "
                "See .env.example for the new sleep/wake config.",
                old_key, new_key,
            )

    for removed_key in ("MORNING_REPORT_HOUR", "EVENING_REPORT_HOUR", "REPORT_MAX_TOKENS"):
        if os.getenv(removed_key):
            _log.warning(
                "[DEPRECATED] %s is no longer used. Morning briefings are now "
                "generated automatically by Think on the first heartbeat tick. "
                "You can safely remove this from .env.",
                removed_key,
            )
