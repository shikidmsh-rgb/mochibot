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
# These env vars are seed data — auto-imported to DB on first startup.
# After that, manage models via the admin portal (DB is the single authority).
# If tier-specific env vars are empty, they fall back to CHAT_* config.

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
# Transport — Telegram
# ═══════════════════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")

# ═══════════════════════════════════════════════════════════════════════════
# Transport — WeChat (optional secondary transport)
# ═══════════════════════════════════════════════════════════════════════════
# Run `python weixin_auth.py` to scan QR code and get your token.

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

HEARTBEAT_INTERVAL_MINUTES = _env_int("HEARTBEAT_INTERVAL_MINUTES", 20)
MAX_DAILY_PROACTIVE = _env_int("MAX_DAILY_PROACTIVE", 10)
PROACTIVE_COOLDOWN_SECONDS = _env_int("PROACTIVE_COOLDOWN_SECONDS", 1800)
THINK_FALLBACK_MINUTES = _env_int("THINK_FALLBACK_MINUTES", 60)
LLM_HEARTBEAT_TIMEOUT_SECONDS = _env_int("LLM_HEARTBEAT_TIMEOUT_SECONDS", 120)
PROACTIVE_CHAT_MAX_TOKENS = _env_int("PROACTIVE_CHAT_MAX_TOKENS", 512)
PROACTIVE_CHAT_HISTORY_TURNS = _env_int("PROACTIVE_CHAT_HISTORY_TURNS", 10)

# Sleep/Wake State Machine
WAKE_EARLIEST_HOUR = _env_int("WAKE_EARLIEST_HOUR", 6)   # don't wake on user msg before this
SLEEP_AFTER_HOUR = _env_int("SLEEP_AFTER_HOUR", 21)      # keyword + silence sleep start
SILENCE_THRESHOLD_HOURS = _env_float("SILENCE_THRESHOLD_HOURS", 1.0)  # silence → sleep
SLEEP_KEYWORDS = _env("SLEEP_KEYWORDS", "晚安,睡了,去睡了,good night,gn").split(",")
SILENCE_PAUSE_DAYS = _env_float("SILENCE_PAUSE_DAYS", 3.0)
FALLBACK_WAKE_HOUR = _env_int("FALLBACK_WAKE_HOUR", 10)
# DEPRECATED — kept for .env backward compat, no longer used by heartbeat
AWAKE_HOUR_START = _env_int("AWAKE_HOUR_START", 7)       # DEPRECATED
AWAKE_HOUR_END = _env_int("AWAKE_HOUR_END", 23)          # DEPRECATED
SLEEP_KEYWORD_HOUR_START = _env_int("SLEEP_KEYWORD_HOUR_START", 21)  # DEPRECATED
SLEEP_KEYWORD_HOUR_END = _env_int("SLEEP_KEYWORD_HOUR_END", 4)      # DEPRECATED
SILENCE_SLEEP_AFTER_HOUR = _env_int("SILENCE_SLEEP_AFTER_HOUR", 23)  # DEPRECATED
SILENCE_SLEEP_THRESHOLD_HOURS = _env_float("SILENCE_SLEEP_THRESHOLD_HOURS", 1.0)  # DEPRECATED

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
# Bedtime Tidy
# ═══════════════════════════════════════════════════════════════════════════

BEDTIME_TIDY_ENABLED = _env_bool("BEDTIME_TIDY_ENABLED", True)
BEDTIME_TIDY_TIMEOUT_S = _env_int("BEDTIME_TIDY_TIMEOUT_S", 60)
BEDTIME_TIDY_MAX_ROUNDS = _env_int("BEDTIME_TIDY_MAX_ROUNDS", 5)
BEDTIME_TIDY_MAX_TOKENS = _env_int("BEDTIME_TIDY_MAX_TOKENS", 1024)
BEDTIME_TIDY_TOOLS = _env(
    "BEDTIME_TIDY_TOOLS",
    "note,todo",
).split(",")

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
# Memory Auto-Recall (pre-turn embedding retrieval)
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_AUTO_RECALL = _env_bool("MEMORY_AUTO_RECALL", True)
MEMORY_AUTO_RECALL_TOP_K = _env_int("MEMORY_AUTO_RECALL_TOP_K", 5)
MEMORY_AUTO_RECALL_MAX_ITEMS = _env_int("MEMORY_AUTO_RECALL_MAX_ITEMS", 3)
MEMORY_AUTO_RECALL_MIN_VEC_SIM = _env_float("MEMORY_AUTO_RECALL_MIN_VEC_SIM", 0.35)
MEMORY_AUTO_RECALL_MIN_SCORE = _env_float("MEMORY_AUTO_RECALL_MIN_SCORE", 0.72)
MEMORY_AUTO_RECALL_MAX_CHARS = _env_int("MEMORY_AUTO_RECALL_MAX_CHARS", 320)
MEMORY_AUTO_RECALL_COOLDOWN = _env_int("MEMORY_AUTO_RECALL_COOLDOWN", 120)

# ═══════════════════════════════════════════════════════════════════════════
# Memory Lifecycle
# ═══════════════════════════════════════════════════════════════════════════

MEMORY_DEMOTE_AFTER_DAYS = _env_int("MEMORY_DEMOTE_AFTER_DAYS", 60)
MEMORY_DEMOTE_MIN_ACCESS = _env_int("MEMORY_DEMOTE_MIN_ACCESS", 3)
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

TOOL_ROUTER_ENABLED = _env_bool("TOOL_ROUTER_ENABLED", True)
TOOL_ROUTER_MAX_TOKENS = _env_int("TOOL_ROUTER_MAX_TOKENS", 300)

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
LOG_LEVEL = _env("LOG_LEVEL", "INFO")            # DEBUG, INFO, WARNING, ERROR, CRITICAL


# ═══════════════════════════════════════════════════════════════════════════
# Startup Validation
# ═══════════════════════════════════════════════════════════════════════════

def _is_private_lan_ip(ip: str) -> bool:
    """Check if an IP is a standard private LAN address (RFC 1918)."""
    return (ip.startswith("192.168.")
            or ip.startswith("10.")
            or any(ip.startswith(f"172.{i}.") for i in range(16, 32)))


def _detect_host_ip() -> str:
    """Best-effort detection of a LAN IP for this machine.

    Prefers RFC 1918 private addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    over other non-loopback IPs, since VPN/proxy software can inject addresses
    like 198.18.x.x that look non-loopback but aren't reachable from the LAN.
    """
    import socket

    candidates: list[str] = []

    # Method 1: UDP connect to public DNS (discovers outbound route IP)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip != "127.0.0.1":
            candidates.append(ip)
    except Exception:
        pass

    # Method 2: all IPs from getaddrinfo (may list multiple interfaces)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip != "127.0.0.1" and ip not in candidates:
                candidates.append(ip)
    except Exception:
        pass

    # Prefer RFC 1918 private LAN IPs
    for ip in candidates:
        if _is_private_lan_ip(ip):
            return ip

    # Fall back to any non-loopback IP
    return candidates[0] if candidates else ""


# Populated by validate_config() when in setup mode — used by /admin command
_DETECTED_HOST: str = ""


def _persist_env_key(key: str, value: str) -> None:
    """Write a key=value into .env (insert or update). Does not raise."""
    env_path = _PROJECT_ROOT / ".env"
    try:
        if env_path.exists():
            lines = env_path.read_text().splitlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}="):
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")
            env_path.write_text("\n".join(lines) + "\n")
        else:
            env_path.write_text(f"{key}={value}\n")
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not persist %s to .env — set it manually", key
        )


def validate_config() -> str:
    """Preflight check at startup.

    Returns:
        "ok"         — all good, start normally
        "setup_mode" — transport configured but no LLM; start in setup mode
    Exits:
        sys.exit(1)  — nothing useful can run (no model AND no transport)
    """
    import sys
    import secrets
    import logging as _logging
    _log = _logging.getLogger(__name__)

    global ADMIN_BIND, ADMIN_TOKEN, _DETECTED_HOST

    # Check if any model is available in DB (seeded from .env or configured via admin portal)
    has_model_db = False
    try:
        from mochi.admin.admin_db import get_tier_effective_config
        for cfg in get_tier_effective_config().values():
            if cfg.get("model") and cfg.get("api_key_set"):
                has_model_db = True
                break
    except Exception:
        pass  # DB not initialized yet or admin_db unavailable

    has_transport = bool(TELEGRAM_BOT_TOKEN) or WEIXIN_ENABLED

    if not has_model_db and has_transport:
        # ── Setup mode: transport ready but no LLM ──
        _log.info("=" * 55)
        _log.info("  SETUP MODE — transport configured, no LLM model yet")
        _log.info("  Send /admin to the bot to get the admin portal URL")
        _log.info("=" * 55)

        # Bind admin to all interfaces so phone can reach it (memory only)
        ADMIN_BIND = "0.0.0.0"

        # Ensure ADMIN_TOKEN exists for secure remote access
        if not ADMIN_TOKEN:
            token = secrets.token_urlsafe(32)
            ADMIN_TOKEN = token
            os.environ["ADMIN_TOKEN"] = token
            _persist_env_key("ADMIN_TOKEN", token)
            _log.info("Generated ADMIN_TOKEN (saved to .env)")

        # Detect server IP for /admin command
        _DETECTED_HOST = _detect_host_ip()
        if _DETECTED_HOST:
            _log.info("Detected server IP: %s", _DETECTED_HOST)

        return "setup_mode"

    if not has_model_db:
        _log.critical(
            "[CRITICAL] MODEL_CONFIG — "
            "No LLM model configured — set CHAT_MODEL in .env or configure via admin portal"
        )
        if not has_transport:
            _log.warning(
                "[WARN] TELEGRAM_BOT_TOKEN / WEIXIN_ENABLED — "
                "No transport configured — bot will not receive messages"
            )
        _log.critical(
            "Critical config missing. Set CHAT_MODEL in .env and restart, "
            "or configure via admin portal."
        )
        sys.exit(1)

    if not has_transport:
        _log.warning(
            "[WARN] TELEGRAM_BOT_TOKEN / WEIXIN_ENABLED — "
            "No transport configured — bot will not receive messages"
        )

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

    return "ok"
