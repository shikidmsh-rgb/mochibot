"""Admin portal — DB helpers for model registry, tier assignments, runtime config.

DB is the runtime source of truth. The portal exposes only product-level
preferences; internal values remain registered for typed legacy overrides.
"""

import logging
import os
import re
from datetime import datetime
from urllib.parse import urlsplit

from mochi.config import (
    TZ,
    MAIN_PROVIDER, MAIN_API_KEY, MAIN_MODEL, MAIN_BASE_URL,
    FREE_TIME_AWAKE_END,
    FREE_TIME_AWAKE_START,
)
from mochi.db import _connect
from mochi.admin.admin_crypto import encrypt_api_key, decrypt_api_key

log = logging.getLogger(__name__)

_VALID_TIERS = frozenset({"lite", "main"})
_TIER_ORDER = ("main", "lite")
_VALID_PROVIDERS = frozenset({"openai", "anthropic"})

__KEEP__ = "__KEEP__"


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _is_supported_model(provider: str, base_url: str) -> bool:
    provider = (provider or "").strip().lower()
    normalized = _normalize_base_url(base_url)
    if provider == "anthropic":
        return not normalized
    if provider != "openai" or not normalized:
        return provider == "openai"
    if re.search(r"[\x00-\x1f\x7f]", normalized):
        return False
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname or ""
        port = parsed.port
        return (
            parsed.scheme == "https"
            and bool(hostname)
            and re.fullmatch(r"[A-Za-z0-9.:-]+", hostname) is not None
            and (port is None or 1 <= port <= 65535)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not parsed.path.rstrip("/").lower().endswith("/chat/completions")
        )
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Model Registry
# ═══════════════════════════════════════════════════════════════════════════

def list_models(*, mask_keys: bool = True) -> list[dict]:
    """List all model registry entries."""
    conn = _connect()
    rows = conn.execute(
        "SELECT name, provider, model, api_key, base_url, created_at, updated_at "
        "FROM model_registry ORDER BY name"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if mask_keys and d.get("api_key"):
            d["api_key"] = "***"
            d["api_key_set"] = True
        elif mask_keys:
            d["api_key_set"] = False
            d["api_key"] = ""
        d["supported"] = _is_supported_model(
            d["provider"], d["base_url"],
        )
        result.append(d)
    return result


def get_model(name: str, *, mask_key: bool = False) -> dict | None:
    """Get a single model registry entry."""
    conn = _connect()
    row = conn.execute(
        "SELECT name, provider, model, api_key, base_url, created_at, updated_at "
        "FROM model_registry WHERE name = ?",
        (name,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if mask_key:
        d["api_key_set"] = bool(d.get("api_key"))
        d["api_key"] = "***" if d.get("api_key") else ""
    else:
        d["api_key"] = decrypt_api_key(d.get("api_key", ""))
    return d


def upsert_model(name: str, provider: str, model: str,
                 api_key: str, base_url: str = "") -> None:
    """Create or update a model registry entry.

    Pass api_key='__KEEP__' to preserve the existing key on update.
    """
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"Invalid provider: {provider!r}")
    if not _is_supported_model(provider, base_url):
        raise ValueError(
            "Invalid endpoint. OpenAI-compatible Base URL must be an HTTPS API "
            "root without credentials, query, fragment, or /chat/completions. "
            "Anthropic uses its official API without a Base URL."
        )
    now = datetime.now(TZ).isoformat()
    conn = _connect()

    if api_key == __KEEP__:
        existing = conn.execute(
            "SELECT api_key FROM model_registry WHERE name = ?", (name,)
        ).fetchone()
        api_key = existing["api_key"] if existing else ""
        # Already encrypted in DB — don't re-encrypt
    else:
        api_key = encrypt_api_key(api_key)

    conn.execute(
        "INSERT INTO model_registry (name, provider, model, api_key, base_url, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "provider=excluded.provider, model=excluded.model, api_key=excluded.api_key, "
        "base_url=excluded.base_url, updated_at=excluded.updated_at",
        (name, provider, model, api_key, base_url, now, now),
    )
    conn.commit()
    conn.close()
    log.info("Upserted model: %s (provider=%s, model=%s)", name, provider, model)


def delete_model(name: str) -> bool:
    """Delete a model. Returns True if existed. Raises if assigned to a tier."""
    conn = _connect()
    # Guard: cannot delete if assigned to a tier
    assigned = conn.execute(
        "SELECT tier FROM tier_assignments WHERE model_name = ?", (name,)
    ).fetchall()
    if assigned:
        conn.close()
        tiers = [r["tier"] for r in assigned]
        raise ValueError(f"Cannot delete model '{name}': assigned to tiers {tiers}")
    cur = conn.execute("DELETE FROM model_registry WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ═══════════════════════════════════════════════════════════════════════════
# Seed from .env (first-run import)
# ═══════════════════════════════════════════════════════════════════════════

def seed_models_from_env() -> None:
    """Import MAIN_* (or resolved legacy CHAT_*) once into an empty registry."""
    if not MAIN_MODEL:
        return

    conn = _connect()
    has_models = conn.execute("SELECT 1 FROM model_registry LIMIT 1").fetchone()
    conn.close()
    if has_models:
        return

    provider = (MAIN_PROVIDER or "openai").strip().lower()
    if not _is_supported_model(provider, MAIN_BASE_URL):
        log.error(
            "MAIN_PROVIDER '%s' is unsupported; configure an OpenAI-compatible "
            "or Anthropic model in the admin portal",
            provider,
        )
        return

    name = MAIN_MODEL
    upsert_model(name, provider, MAIN_MODEL, MAIN_API_KEY, MAIN_BASE_URL)
    set_tier_assignment("main", name)
    log.info("Seeded MAIN_* model '%s' for the main tier", name)


# ═══════════════════════════════════════════════════════════════════════════
# Tier Assignments
# ═══════════════════════════════════════════════════════════════════════════

def list_tier_assignments() -> dict[str, str]:
    """Return {tier: model_name} for all DB-assigned tiers."""
    conn = _connect()
    rows = conn.execute("SELECT tier, model_name FROM tier_assignments").fetchall()
    conn.close()
    return {r["tier"]: r["model_name"] for r in rows}


def set_tier_assignment(tier: str, model_name: str) -> None:
    """Assign a model registry entry to a tier."""
    if tier not in _VALID_TIERS:
        raise ValueError(f"Invalid tier: {tier!r}")
    # Verify model exists
    conn = _connect()
    entry = conn.execute(
        "SELECT provider, base_url FROM model_registry WHERE name = ?", (model_name,)
    ).fetchone()
    if not entry:
        conn.close()
        raise ValueError(f"Model '{model_name}' not found in registry")
    if not _is_supported_model(entry["provider"], entry["base_url"]):
        conn.close()
        raise ValueError(
            f"Model '{model_name}' uses unsupported provider "
            f"{entry['provider']!r}; reconfigure it in the admin portal"
        )
    now = datetime.now(TZ).isoformat()
    conn.execute(
        "INSERT INTO tier_assignments (tier, model_name, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(tier) DO UPDATE SET model_name=excluded.model_name, updated_at=excluded.updated_at",
        (tier, model_name, now),
    )
    conn.commit()
    conn.close()
    log.info("Assigned tier '%s' → model '%s'", tier, model_name)


def clear_tier_assignment(tier: str) -> None:
    """Reject clearing required product tier assignments."""
    if tier not in _VALID_TIERS:
        raise ValueError(f"Invalid tier: {tier!r}")
    raise ValueError(
        f"{tier.capitalize()} is required; assign another model instead"
    )


def get_tier_effective_config() -> dict[str, dict]:
    """For each tier, return effective config from DB.

    Returns {tier: {provider, model, base_url, api_key_set, assigned_name}}.
    ``assigned_name`` is the model-registry name if assigned, empty string otherwise.
    """
    assignments = list_tier_assignments()
    result: dict[str, dict] = {}

    for tier in _TIER_ORDER:
        if tier in assignments:
            model_entry = get_model(assignments[tier], mask_key=False)
            if model_entry:
                result[tier] = {
                    "provider": model_entry["provider"],
                    "model": model_entry["model"],
                    "base_url": model_entry["base_url"],
                    "api_key": model_entry["api_key"],
                    "api_key_set": bool(model_entry["api_key"]),
                    "assigned_name": assignments[tier],
                    "supported": _is_supported_model(
                        model_entry["provider"], model_entry["base_url"],
                    ),
                    "fallback_from": "",
                }
                continue

        # No DB assignment — tier is unconfigured
        result[tier] = {
            "provider": "", "model": "", "base_url": "",
            "api_key": "", "api_key_set": False, "assigned_name": "",
            "supported": False, "fallback_from": "",
        }

    return result


def is_main_tier_ready(tier_config: dict[str, dict] | None = None) -> bool:
    """Return whether the main tier has a usable explicit assignment."""
    config = tier_config if tier_config is not None else get_tier_effective_config()
    main = config.get("main", {})
    return bool(
        main.get("assigned_name")
        and main.get("supported")
        and main.get("model")
        and main.get("api_key_set")
    )


def are_required_tiers_ready(
    tier_config: dict[str, dict] | None = None,
) -> bool:
    """Return whether Main and Lite both have usable explicit assignments."""
    config = tier_config if tier_config is not None else get_tier_effective_config()
    return all(
        config.get(tier, {}).get("assigned_name")
        and config[tier].get("supported")
        and config[tier].get("model")
        and config[tier].get("api_key_set")
        for tier in _TIER_ORDER
    )


# ═══════════════════════════════════════════════════════════════════════════
# System Overrides (heartbeat config via skill_config)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_SKILL_NAME = "_system"

# Runtime configuration registry with (type, default_value).
# Only a small preference subset is user-facing; the rest remain here so legacy
# DB overrides keep their original types and behavior.
# Environment-only keys:
#   PROACTIVE_COOLDOWN_SECONDS, THINK_FALLBACK_MINUTES, LLM_HEARTBEAT_TIMEOUT_SECONDS — internal heartbeat tuning
#   BEDTIME_ENTRY_ENABLED, BEDTIME_ENTRY_TIMEOUT_S — no dedicated settings page
#   Autonomous Main Runtime output tuning
_ENV_ONLY_SYSTEM_KEYS = frozenset({
    "PROACTIVE_COOLDOWN_SECONDS",
    "THINK_FALLBACK_MINUTES",
    "LLM_HEARTBEAT_TIMEOUT_SECONDS",
})
_DEPRECATED_SYSTEM_KEYS = frozenset({
    "MAX_DAILY_PROACTIVE",
    "FREE_TIME_MIN_MINUTES",
    "FREE_TIME_MAX_MINUTES",
})

SYSTEM_DEFAULTS: dict[str, tuple[str, any]] = {
    # ── Heartbeat ──
    "HEARTBEAT_INTERVAL_MINUTES":     ("int",   20),
    "MAX_DAILY_FREE_TIME":            ("int",   32),
    "ATTENTION_INTERVAL_MINUTES":     ("int",   60),
    "FALLBACK_WAKE_HOUR":             ("int",   10),
    "BEDTIME_ENTRY_ENABLED":          ("bool",  True),
    "BEDTIME_ENTRY_TIMEOUT_S":        ("int",   60),
    # ── Sleep/Wake ──
    "WAKE_EARLIEST_HOUR":             ("int",   8),
    "SLEEP_AFTER_HOUR":               ("int",   1),
    "FREE_TIME_AWAKE_START":          ("str",   FREE_TIME_AWAKE_START),
    "FREE_TIME_AWAKE_END":            ("str",   FREE_TIME_AWAKE_END),
    "SILENCE_PAUSE_DAYS":             ("float", 3.0),
    # ── Basic ──
    "TIMEZONE_OFFSET_HOURS":          ("float", 8.0),
    "AI_CHAT_MAX_COMPLETION_TOKENS":  ("int",   4096),
    "MAINTENANCE_HOUR":               ("int",   3),
    "MAINTENANCE_ENABLED":            ("bool",  True),
    "WEEKLY_MAINTENANCE_ENABLED":     ("bool",  True),
    "WEEKLY_MAINTENANCE_MINUTE":      ("int",   15),
}


def _cast_system(raw: str, type_name: str):
    """Cast a DB string to the declared system config type."""
    if type_name == "bool":
        return raw.lower() in ("true", "1", "yes")
    if type_name == "int":
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 0
    if type_name == "float":
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0
    return raw


# ── Cached system config reader ──────────────────────────────────────────

_system_config_cache: dict[str, str] = {}
_system_config_cache_time: float = 0.0


def get_system_config(key: str):
    """Get effective system config value from DB with 60s cache.

    Priority: DB value > SYSTEM_DEFAULTS > config module fallback.
    """
    global _system_config_cache, _system_config_cache_time
    import time as _time
    now = _time.monotonic()
    if now - _system_config_cache_time > 60:
        try:
            _system_config_cache = get_system_overrides()
        except Exception:
            _system_config_cache = {}
        _system_config_cache_time = now

    raw = _system_config_cache.get(key)
    if raw is not None:
        type_name = SYSTEM_DEFAULTS.get(key, ("str",))[0]
        return _cast_system(raw, type_name)

    if key in SYSTEM_DEFAULTS:
        return SYSTEM_DEFAULTS[key][1]

    import mochi.config as _cfg
    if key not in _ENV_ONLY_SYSTEM_KEYS:
        log.warning("get_system_config: unknown key %r, falling back to config module", key)
    return getattr(_cfg, key, None)


def invalidate_system_config_cache() -> None:
    """Force next get_system_config() call to re-read from DB."""
    global _system_config_cache_time
    _system_config_cache_time = 0.0


# ── Seed system config from .env ─────────────────────────────────────────

def seed_system_config_from_env() -> None:
    """Import explicit .env values only when the DB setting is missing.

    Existing DB values are authoritative. Hardcoded defaults stay in code
    rather than being copied into the database.
    """
    from mochi.admin.admin_env import read_env_file

    conn = _connect()
    existing_new = conn.execute(
        "SELECT 1 FROM skill_config WHERE skill_name = ? "
        "AND key = 'MAX_DAILY_FREE_TIME'",
        (_SYSTEM_SKILL_NAME,),
    ).fetchone()
    legacy = conn.execute(
        "SELECT value FROM skill_config WHERE skill_name = ? "
        "AND key = 'MAX_DAILY_PROACTIVE'",
        (_SYSTEM_SKILL_NAME,),
    ).fetchone()
    env_file = read_env_file()
    legacy_value = (
        legacy["value"]
        if legacy is not None
        else env_file.get("MAX_DAILY_PROACTIVE")
        if env_file.get("MAX_DAILY_PROACTIVE") is not None
        else os.environ.get("MAX_DAILY_PROACTIVE")
    )
    if existing_new is None and str(legacy_value or "").strip() == "0":
        now = datetime.now(TZ).isoformat()
        conn.execute(
            "INSERT INTO skill_config (skill_name, key, value, updated_at) "
            "VALUES (?, 'MAX_DAILY_FREE_TIME', '0', ?)",
            (_SYSTEM_SKILL_NAME, now),
        )
    cleared_keys = _ENV_ONLY_SYSTEM_KEYS | _DEPRECATED_SYSTEM_KEYS
    placeholders = ",".join("?" for _ in cleared_keys)
    deleted = conn.execute(
        f"DELETE FROM skill_config WHERE skill_name = ? AND key IN ({placeholders})",
        [_SYSTEM_SKILL_NAME] + list(cleared_keys),
    ).rowcount
    conn.commit()
    conn.close()
    if deleted:
        log.info("Cleared %d env-only/deprecated system overrides from DB", deleted)
        invalidate_system_config_cache()

    existing = get_system_overrides()
    env_file = read_env_file()
    seeded = 0
    for key in SYSTEM_DEFAULTS:
        env_raw = env_file.get(key)
        if env_raw is None and key in os.environ:
            env_raw = os.environ[key]
        if key not in existing and env_raw is not None:
            set_system_override(key, env_raw)
            seeded += 1

    if seeded:
        log.info("Config seed: %d explicit value(s) imported", seeded)


def get_system_overrides() -> dict[str, str]:
    """Get all system overrides from skill_config table."""
    conn = _connect()
    rows = conn.execute(
        "SELECT key, value FROM skill_config WHERE skill_name = ?",
        (_SYSTEM_SKILL_NAME,),
    ).fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_system_override(key: str, value: str) -> None:
    """Set a system override in skill_config."""
    now = datetime.now(TZ).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO skill_config (skill_name, key, value, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(skill_name, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_SYSTEM_SKILL_NAME, key, value, now),
    )
    conn.commit()
    conn.close()
    invalidate_system_config_cache()
