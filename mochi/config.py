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
# LLM — Main Model (required)
# ═══════════════════════════════════════════════════════════════════════════
# Runtime model configuration lives in the DB registry. MAIN_* only seeds a
# first installation.
MAIN_PROVIDER = _env("MAIN_PROVIDER", "openai")
MAIN_API_KEY = _env("MAIN_API_KEY")
MAIN_MODEL = _env("MAIN_MODEL")
MAIN_BASE_URL = _env("MAIN_BASE_URL")

# ═══════════════════════════════════════════════════════════════════════════
# Embedding (vector memory search)
# ═══════════════════════════════════════════════════════════════════════════

# Embedding is optional and uses the same OpenAI-compatible adapter for the
# OpenAI, Alibaba Cloud Bailian, and Azure AI Foundry presets.
EMBEDDING_PROVIDER = _env("EMBEDDING_PROVIDER", "none")    # none | openai
EMBEDDING_API_KEY = _env("EMBEDDING_API_KEY")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL")
EMBEDDING_BASE_URL = _env("EMBEDDING_BASE_URL")

EMBEDDING_CACHE_MAX_SIZE = _env_int("EMBEDDING_CACHE_MAX_SIZE", 128)
EMBEDDING_CACHE_TTL_S = _env_int("EMBEDDING_CACHE_TTL_S", 300)

# ═══════════════════════════════════════════════════════════════════════════
# Transport — Telegram
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")

# ═══════════════════════════════════════════════════════════════════════════
# Transport — WeChat (optional secondary transport)
# ═══════════════════════════════════════════════════════════════════════════
# Run `python scripts/weixin_auth.py` to scan QR code and get your token.

WEIXIN_ENABLED = _env_bool("WEIXIN_ENABLED", False)
WEIXIN_BOT_TOKEN = _env("WEIXIN_BOT_TOKEN")
WEIXIN_BASE_URL = _env("WEIXIN_BASE_URL", "https://ilinkai.weixin.qq.com")
WEIXIN_ALLOWED_USERS = [u.strip() for u in _env("WEIXIN_ALLOWED_USERS").split(",") if u.strip()]
WEIXIN_POLL_TIMEOUT_S = _env_int("WEIXIN_POLL_TIMEOUT_S", 35)
WEIXIN_BUBBLE_DELAY_S = _env_float("WEIXIN_BUBBLE_DELAY_S", 1.0)
WEIXIN_MSG_LIMIT = _env_int("WEIXIN_MSG_LIMIT", 4000)
WEIXIN_BACKOFF_MIN_S = _env_int("WEIXIN_BACKOFF_MIN_S", 2)
WEIXIN_BACKOFF_MAX_S = _env_int("WEIXIN_BACKOFF_MAX_S", 30)
WEIXIN_MAX_CONSECUTIVE_FAILURES = _env_int("WEIXIN_MAX_CONSECUTIVE_FAILURES", 3)
WEIXIN_SESSION_EXPIRED_RETRY_S = _env_int("WEIXIN_SESSION_EXPIRED_RETRY_S", 300)

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

MAX_DAILY_PROACTIVE = _env_int("MAX_DAILY_PROACTIVE", 5)
FREE_TIME_ENABLED = _env_bool("FREE_TIME_ENABLED", True)
LLM_HEARTBEAT_TIMEOUT_SECONDS = _env_int("LLM_HEARTBEAT_TIMEOUT_SECONDS", 120)

# Sleep/Wake State Machine
WAKE_EARLIEST_HOUR = _env_int("WAKE_EARLIEST_HOUR", 6)   # don't wake on user msg before this
SLEEP_AFTER_HOUR = _env_int("SLEEP_AFTER_HOUR", 23)      # bedtime availability + silence checks
SILENCE_THRESHOLD_HOURS = _env_float("SILENCE_THRESHOLD_HOURS", 1.0)  # silence → sleep
SILENCE_PAUSE_DAYS = _env_float("SILENCE_PAUSE_DAYS", 3.0)
FALLBACK_WAKE_HOUR = _env_int("FALLBACK_WAKE_HOUR", 10)

# ═══════════════════════════════════════════════════════════════════════════
# Conversation Summary (continuous complete-turn digest)
# ═══════════════════════════════════════════════════════════════════════════

MAX_HISTORY_TURNS: int = _env_int("MAX_HISTORY_TURNS", 10)
CONV_SUMMARY_BATCH_TURNS: int = max(
    1, _env_int("CONV_SUMMARY_BATCH_TURNS", 20),
)
CONV_SUMMARY_MAX_TOKENS: int = max(
    100, _env_int("CONV_SUMMARY_MAX_TOKENS", 300),
)

# ═══════════════════════════════════════════════════════════════════════════
# Memory
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_EXTRACTION_BATCH_TURNS: int = max(
    1, _env_int("MEMORY_EXTRACTION_BATCH_TURNS", 10),
)
CORE_MAX_TOKENS = _env_int("CORE_MAX_TOKENS", 1400)
TRASH_PURGE_DAYS = _env_int("TRASH_PURGE_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Maintenance
# ═══════════════════════════════════════════════════════════════════════════

MAINTENANCE_HOUR = _env_int("MAINTENANCE_HOUR", 3)
MAINTENANCE_ENABLED = _env_bool("MAINTENANCE_ENABLED", True)
WEEKLY_MAINTENANCE_ENABLED = _env_bool("WEEKLY_MAINTENANCE_ENABLED", True)
WEEKLY_MAINTENANCE_MINUTE = _env_int("WEEKLY_MAINTENANCE_MINUTE", 15)

# ═══════════════════════════════════════════════════════════════════════════
# Bedtime Main entry
# ═══════════════════════════════════════════════════════════════════════════

BEDTIME_ENTRY_ENABLED = _env_bool("BEDTIME_ENTRY_ENABLED", True)
BEDTIME_ENTRY_TIMEOUT_S = _env_int("BEDTIME_ENTRY_TIMEOUT_S", 60)

# ═══════════════════════════════════════════════════════════════════════════
# Diary
# ═══════════════════════════════════════════════════════════════════════════

DIARY_STATUS_MAX_LINES = _env_int("DIARY_STATUS_MAX_LINES", 20)
DIARY_ENTRY_MAX_LINES = _env_int("DIARY_ENTRY_MAX_LINES", 50)

# Hour (0-23) after which an unlogged meal shows as pending (⏳) in diary status.
MEAL_REMINDER_BREAKFAST_HOUR = _env_int("MEAL_REMINDER_BREAKFAST_HOUR", 10)
MEAL_REMINDER_LUNCH_HOUR = _env_int("MEAL_REMINDER_LUNCH_HOUR", 14)
MEAL_REMINDER_DINNER_HOUR = _env_int("MEAL_REMINDER_DINNER_HOUR", 19)

# ═══════════════════════════════════════════════════════════════════════════
# Optional Integrations
# ═══════════════════════════════════════════════════════════════════════════

WEB_SEARCH_TIMEOUT_S = _env_int("WEB_SEARCH_TIMEOUT_S", 20)

# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

DB_PATH = _PROJECT_ROOT / "data" / "mochi.db"

HEARTBEAT_LOG_TRIM_DAYS = _env_int("HEARTBEAT_LOG_TRIM_DAYS", 7)
HEARTBEAT_LOG_DELETE_DAYS = _env_int("HEARTBEAT_LOG_DELETE_DAYS", 30)

# ═══════════════════════════════════════════════════════════════════════════
# Timezone (default UTC, override for your locale in .env or admin portal)
# ═══════════════════════════════════════════════════════════════════════════

TIMEZONE_OFFSET_HOURS = _env_float("TIMEZONE_OFFSET_HOURS", 8.0)  # seed/fallback only

from datetime import datetime, timedelta, tzinfo


class _TZProxy(tzinfo):
    """Dynamic timezone that re-reads admin DB on each call.

    Caller-side semantics are unchanged: `datetime.now(TZ)` and
    `dt.replace(tzinfo=TZ)` work as before. The actual offset is resolved
    per-call via admin_db.get_system_config (60s cache), so changes from
    the admin portal take effect within ~60s without restart.
    """

    def _hours(self) -> float:
        try:
            from mochi.admin.admin_db import get_system_config  # lazy: avoid circular import
            v = get_system_config("TIMEZONE_OFFSET_HOURS")
            if v is None:
                return TIMEZONE_OFFSET_HOURS
            return float(v)
        except Exception:
            return TIMEZONE_OFFSET_HOURS

    def utcoffset(self, dt):
        return timedelta(hours=self._hours())

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return f"UTC{self._hours():+g}"

    def __repr__(self) -> str:
        return f"_TZProxy({self._hours():+g})"


TZ = _TZProxy()


def _effective_maintenance_hour() -> int:
    try:
        from mochi.admin.admin_db import get_system_config
        v = get_system_config("MAINTENANCE_HOUR")
        if v is None:
            return MAINTENANCE_HOUR
        return int(v)
    except Exception:
        return MAINTENANCE_HOUR


def logical_today(now: datetime | None = None) -> str:
    """Return today's date as YYYY-MM-DD, rolling over at MAINTENANCE_HOUR.

    Before MAINTENANCE_HOUR, entries belong to "yesterday" (the previous
    logical day). This keeps nightly archival and day boundaries consistent.
    """
    if now is None:
        now = datetime.now(TZ)
    if now.hour < _effective_maintenance_hour():
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def logical_days_ago(n: int, now: datetime | None = None) -> str:
    """Return the logical day N days before today (after MAINTENANCE_HOUR rollover).

    Use this for any back-walk over logical days (streaks, period lists, cutoffs).
    Routing back-walks through this helper prevents copy-pasting a wall-clock
    pattern and re-introducing the maintenance-window misalignment bug.
    """
    if now is None:
        now = datetime.now(TZ)
    if now.hour < _effective_maintenance_hour():
        now = now - timedelta(days=1)
    return (now - timedelta(days=n)).strftime("%Y-%m-%d")

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
# Memory Auto-Recall (pre-turn embedding retrieval)
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_AUTO_RECALL = _env_bool("MEMORY_AUTO_RECALL", True)
MEMORY_AUTO_RECALL_TOP_K = _env_int("MEMORY_AUTO_RECALL_TOP_K", 5)
MEMORY_AUTO_RECALL_MAX_ITEMS = _env_int("MEMORY_AUTO_RECALL_MAX_ITEMS", 3)
MEMORY_AUTO_RECALL_MIN_VEC_SIM = _env_float("MEMORY_AUTO_RECALL_MIN_VEC_SIM", 0.35)
MEMORY_AUTO_RECALL_MAX_CHARS = _env_int("MEMORY_AUTO_RECALL_MAX_CHARS", 320)
MEMORY_AUTO_RECALL_MAX_TOKENS = _env_int("MEMORY_AUTO_RECALL_MAX_TOKENS", 600)
MEMORY_AUTO_RECALL_COOLDOWN = _env_int("MEMORY_AUTO_RECALL_COOLDOWN", 0)

# ═══════════════════════════════════════════════════════════════════════════
# Memory Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

RECALL_DECAY_HALF_LIFE_DAYS = _env_float("RECALL_DECAY_HALF_LIFE_DAYS", 30.0)

# ═══════════════════════════════════════════════════════════════════════════
# Knowledge Graph
# ═══════════════════════════════════════════════════════════════════════════

KG_ENABLED = _env_bool("KG_ENABLED", True)
KG_MAX_ENTITY_CONTEXT_TOKENS = _env_int("KG_MAX_ENTITY_CONTEXT_TOKENS", 300)
KG_ENTITY_MATCH_MIN_LENGTH = _env_int("KG_ENTITY_MATCH_MIN_LENGTH", 2)
KG_MAX_TRIPLES_PER_ENTITY = _env_int("KG_MAX_TRIPLES_PER_ENTITY", 20)

# ═══════════════════════════════════════════════════════════════════════════
# Token Limits
# ═══════════════════════════════════════════════════════════════════════════

AI_CHAT_MAX_COMPLETION_TOKENS = _env_int("AI_CHAT_MAX_COMPLETION_TOKENS", 4096)
# Empty keeps the provider default; currently applied only to GPT-6 Sol.
REASONING_EFFORT = _env("REASONING_EFFORT").strip().lower()
DEFAULT_TOOL_LOOP_MAX_ROUNDS = 16
TOOL_LOOP_MAX_ROUNDS = _env_int("TOOL_LOOP_MAX_ROUNDS", DEFAULT_TOOL_LOOP_MAX_ROUNDS)
TOOL_LOOP_TOTAL_TOOL_LIMIT = max(
    0, _env_int("TOOL_LOOP_TOTAL_TOOL_LIMIT", 24),
)
TOOL_LOOP_PER_TOOL_LIMIT = max(
    0, _env_int("TOOL_LOOP_PER_TOOL_LIMIT", 3),
)

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

TOOL_ROUTER_ENABLED = _env_bool("TOOL_ROUTER_ENABLED", True)
TOOL_ROUTER_MAX_TOKENS = _env_int("TOOL_ROUTER_MAX_TOKENS", 300)
# Routing is an optimization; Main answers without it after this wait.
TOOL_ROUTER_TIMEOUT_S = _env_float("TOOL_ROUTER_TIMEOUT_S", 8.0)
# Ordinary owner chat keeps non-resident tools loaded while messages continue
# within this idle gap, so follow-ups keep their tools and a stable schema.
TOOL_SESSION_IDLE_MINUTES = _env_int("TOOL_SESSION_IDLE_MINUTES", 30)
# A carried toolbox larger than this restarts from the current turn's tools.
TOOL_SESSION_MAX_EXTRA_TOOLS = _env_int("TOOL_SESSION_MAX_EXTRA_TOOLS", 24)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Escalation
# ═══════════════════════════════════════════════════════════════════════════

TOOL_ESCALATION_ENABLED = _env_bool("TOOL_ESCALATION_ENABLED", True)
TOOL_ESCALATION_MAX_PER_TURN = _env_int("TOOL_ESCALATION_MAX_PER_TURN", 4)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Governance
# ═══════════════════════════════════════════════════════════════════════════

TOOL_DENY_NAMES = _env("TOOL_DENY_NAMES", "")           # comma-separated denylist
TOOL_RATE_LIMIT_PER_MIN = _env_int("TOOL_RATE_LIMIT_PER_MIN", 10)

# ═══════════════════════════════════════════════════════════════════════════
# Chatty Rhythm
# ═══════════════════════════════════════════════════════════════════════════

BUBBLE_ENABLED = _env_bool("BUBBLE_ENABLED", True)
TG_INTERIM_ENABLED = _env_bool("TG_INTERIM_ENABLED", True)
TG_BUBBLE_DELAY_S = _env_float("TG_BUBBLE_DELAY_S", 1.0)
TG_BUBBLE_MAX = _env_int("TG_BUBBLE_MAX", 8)
TG_BUBBLE_DELIMITER = _env("TG_BUBBLE_DELIMITER", "|||")
TG_BUBBLE_MIN_CHARS = _env_int("TG_BUBBLE_MIN_CHARS", 8)
TG_AGGREGATE_ENABLED = _env_bool("TG_AGGREGATE_ENABLED", True)

# Tool-call status UX (Telegram DM only)
TG_STATUS_REACTIONS_ENABLED = _env_bool("TG_STATUS_REACTIONS_ENABLED", True)
TG_STATUS_MESSAGE_ENABLED = _env_bool("TG_STATUS_MESSAGE_ENABLED", True)
TG_STATUS_EDIT_INTERVAL_S = _env_float("TG_STATUS_EDIT_INTERVAL_S", 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Admin Portal
# ═══════════════════════════════════════════════════════════════════════════

ADMIN_ENABLED = _env_bool("ADMIN_ENABLED", True)
ADMIN_PORT = _env_int("ADMIN_PORT", 8080)
ADMIN_BIND = _env("ADMIN_BIND", "127.0.0.1")   # default localhost-only; set 0.0.0.0 for remote access
ADMIN_TOKEN = _env("ADMIN_TOKEN")               # optional; if set, all requests must include this token
LOG_LEVEL = _env("LOG_LEVEL", "INFO")            # DEBUG, INFO, WARNING, ERROR, CRITICAL


# ═══════════════════════════════════════════════════════════════════════════
# Startup Validation
# ═══════════════════════════════════════════════════════════════════════════

def validate_config() -> str:
    """Return normal, guided setup, or admin-only startup mode."""
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # Normal runtime requires explicit Main and Lite assignments.
    has_required_models = False
    try:
        from mochi.admin.admin_db import are_required_tiers_ready
        has_required_models = are_required_tiers_ready()
    except Exception:
        pass  # DB not initialized yet or admin_db unavailable

    has_transport = bool(TELEGRAM_BOT_TOKEN) or WEIXIN_ENABLED
    mode = "ok"

    if has_transport and not has_required_models:
        mode = "setup_mode"
        _log.info("=" * 55)
        _log.info("  SETUP MODE — waiting for explicit Main and Lite models")
        _log.info("  Open the local admin portal to finish setup")
        _log.info("=" * 55)
    elif not has_transport:
        mode = "admin_only"
        _log.warning(
            "[WARN] TELEGRAM_BOT_TOKEN / WEIXIN_ENABLED — "
            "No transport configured — bot will not receive messages"
        )

    return mode
