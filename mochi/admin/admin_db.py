"""Admin portal — DB helpers for model registry, tier assignments, system config.

DB is the single source of truth for all admin-configurable settings.
.env vars are seed data — auto-imported to DB on first startup, then DB-only at runtime.
"""

import logging
from datetime import datetime, timezone, timedelta

from mochi.config import (
    DB_PATH, TIMEZONE_OFFSET_HOURS,
    CHAT_PROVIDER, CHAT_API_KEY, CHAT_MODEL, CHAT_BASE_URL,
    TIER_LITE_PROVIDER, TIER_LITE_API_KEY, TIER_LITE_MODEL, TIER_LITE_BASE_URL,
    TIER_CHAT_PROVIDER, TIER_CHAT_API_KEY, TIER_CHAT_MODEL, TIER_CHAT_BASE_URL,
    TIER_DEEP_PROVIDER, TIER_DEEP_API_KEY, TIER_DEEP_MODEL, TIER_DEEP_BASE_URL,
)
from mochi.db import _connect
from mochi.admin.admin_crypto import encrypt_api_key, decrypt_api_key

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

_VALID_TIERS = frozenset({"lite", "chat", "deep"})
_VALID_PROVIDERS = frozenset({"openai", "azure_openai", "anthropic"})

__KEEP__ = "__KEEP__"


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
    """Import .env model config into DB on first startup.

    - If model_registry is empty and CHAT_MODEL is set: create model entries
      from CHAT_* and any differing TIER_* env vars, then assign all tiers.
    - If model_registry has entries but tier_assignments is incomplete:
      fill missing tiers from env config.
    - If everything is already populated: no-op.
    """
    if not CHAT_MODEL:
        return  # user hasn't configured .env yet

    conn = _connect()
    has_models = conn.execute("SELECT 1 FROM model_registry LIMIT 1").fetchone()
    existing_tiers = {
        r["tier"] for r in conn.execute("SELECT tier FROM tier_assignments").fetchall()
    }
    conn.close()

    if has_models and existing_tiers >= _VALID_TIERS:
        # Phase 2: sync .env changes into DB (mirrors seed_system_config_from_env)
        _sync_env_models_to_db()
        return

    # Build per-tier env config: (provider, api_key, model, base_url)
    tier_env = {
        "lite": (
            TIER_LITE_PROVIDER or CHAT_PROVIDER,
            TIER_LITE_API_KEY or CHAT_API_KEY,
            TIER_LITE_MODEL or CHAT_MODEL,
            TIER_LITE_BASE_URL or CHAT_BASE_URL,
        ),
        "chat": (
            TIER_CHAT_PROVIDER or CHAT_PROVIDER,
            TIER_CHAT_API_KEY or CHAT_API_KEY,
            TIER_CHAT_MODEL or CHAT_MODEL,
            TIER_CHAT_BASE_URL or CHAT_BASE_URL,
        ),
        "deep": (
            TIER_DEEP_PROVIDER or CHAT_PROVIDER,
            TIER_DEEP_API_KEY or CHAT_API_KEY,
            TIER_DEEP_MODEL or CHAT_MODEL,
            TIER_DEEP_BASE_URL or CHAT_BASE_URL,
        ),
    }

    # Dedup: same (provider, key, model, base_url) → same registry name
    seen: dict[tuple, str] = {}  # config tuple → model name
    models_created = 0
    tiers_assigned = 0

    for tier in sorted(_VALID_TIERS):
        if tier in existing_tiers:
            continue  # already assigned

        provider, api_key, model, base_url = tier_env[tier]
        if not model:
            continue

        config_key = (provider, api_key, model, base_url)
        if config_key in seen:
            name = seen[config_key]
        else:
            # Derive a name: prefer the model string itself, ensure uniqueness
            name = model
            # Check if name already exists in registry with different config
            existing = get_model(name, mask_key=False)
            if existing and (existing["provider"] != provider or
                            existing["model"] != model):
                name = f"{model}-{tier}"
            if not has_models or not existing:
                upsert_model(name, provider, model, api_key, base_url)
                models_created += 1
            seen[config_key] = name

        set_tier_assignment(tier, name)
        tiers_assigned += 1

    if models_created or tiers_assigned:
        log.info("Seeded model config from .env: %d model(s), %d tier(s)",
                 models_created, tiers_assigned)


# ── .env ↔ DB sync helpers ─────────────────────────────────────────────

_ENV_FIELD_MAP = {"provider": "PROVIDER", "api_key": "API_KEY",
                  "model": "MODEL", "base_url": "BASE_URL"}


def _sync_env_models_to_db() -> None:
    """Phase 2 sync: if .env model keys differ from DB, update DB.

    Same pattern as seed_system_config_from_env Phase 2: .env wins when
    a key is physically present and differs from DB.
    """
    from mochi.admin.admin_env import read_env_file

    env_file = read_env_file()
    if not env_file.get("CHAT_MODEL"):
        return  # no model config in .env — don't touch DB

    # Build per-tier config from .env (same logic as seed)
    def _env(key: str) -> str:
        return env_file.get(key, "")

    tier_env = {
        "lite": (
            _env("TIER_LITE_PROVIDER") or _env("CHAT_PROVIDER"),
            _env("TIER_LITE_API_KEY") or _env("CHAT_API_KEY"),
            _env("TIER_LITE_MODEL") or _env("CHAT_MODEL"),
            _env("TIER_LITE_BASE_URL") or _env("CHAT_BASE_URL"),
        ),
        "chat": (
            _env("TIER_CHAT_PROVIDER") or _env("CHAT_PROVIDER"),
            _env("TIER_CHAT_API_KEY") or _env("CHAT_API_KEY"),
            _env("TIER_CHAT_MODEL") or _env("CHAT_MODEL"),
            _env("TIER_CHAT_BASE_URL") or _env("CHAT_BASE_URL"),
        ),
        "deep": (
            _env("TIER_DEEP_PROVIDER") or _env("CHAT_PROVIDER"),
            _env("TIER_DEEP_API_KEY") or _env("CHAT_API_KEY"),
            _env("TIER_DEEP_MODEL") or _env("CHAT_MODEL"),
            _env("TIER_DEEP_BASE_URL") or _env("CHAT_BASE_URL"),
        ),
    }

    effective = get_tier_effective_config()
    seen: dict[tuple, str] = {}  # config tuple → model name
    synced = 0

    for tier in sorted(_VALID_TIERS):
        provider, api_key, model, base_url = tier_env[tier]
        if not model:
            continue

        # Compare with current DB config
        db_cfg = effective.get(tier, {})
        if (db_cfg.get("provider") == provider
                and db_cfg.get("model") == model
                and db_cfg.get("api_key") == api_key
                and db_cfg.get("base_url", "") == base_url):
            continue  # no diff

        config_key = (provider, api_key, model, base_url)
        if config_key in seen:
            name = seen[config_key]
        else:
            # Derive name from model string (same as seed logic)
            name = model
            existing = get_model(name, mask_key=False)
            if existing and (existing["provider"] != provider or
                            existing["model"] != model):
                name = f"{model}-{tier}"
            upsert_model(name, provider, model, api_key, base_url)
            seen[config_key] = name

        set_tier_assignment(tier, name)
        synced += 1
        log.info("Synced tier '%s' from .env: model=%s provider=%s",
                 tier, model, provider)

    if synced:
        log.info("Phase 2 model sync: %d tier(s) updated from .env", synced)


def sync_models_to_env() -> None:
    """Write current tier model config from DB to .env for persistence.

    Called after portal model/tier changes so .env stays in sync.
    Convention: chat tier → CHAT_*, lite/deep → TIER_X_* only if different.
    TIER_CHAT_* is always cleared to avoid precedence conflicts at seed time.
    """
    from mochi.admin.admin_env import write_env_value, remove_env_key

    effective = get_tier_effective_config()
    chat_cfg = effective.get("chat", {})

    # Write CHAT_* from chat tier (the baseline)
    for field, suffix in _ENV_FIELD_MAP.items():
        val = chat_cfg.get(field, "")
        key = f"CHAT_{suffix}"
        try:
            if val:
                write_env_value(key, val)
            else:
                remove_env_key(key)
        except (PermissionError, ValueError, OSError) as e:
            log.warning("sync_models_to_env: failed to write %s: %s", key, e)

    # Always clear TIER_CHAT_* to avoid TIER_CHAT > CHAT precedence at seed
    for suffix in _ENV_FIELD_MAP.values():
        key = f"TIER_CHAT_{suffix}"
        try:
            remove_env_key(key)
        except (PermissionError, ValueError, OSError) as e:
            log.warning("sync_models_to_env: failed to remove %s: %s", key, e)

    # Write TIER_X_* only for tiers that differ from chat
    for tier in ("lite", "deep"):
        tier_cfg = effective.get(tier, {})
        prefix = f"TIER_{tier.upper()}"
        for field, suffix in _ENV_FIELD_MAP.items():
            tier_val = tier_cfg.get(field, "")
            chat_val = chat_cfg.get(field, "")
            key = f"{prefix}_{suffix}"
            try:
                if tier_val and tier_val != chat_val:
                    write_env_value(key, tier_val)
                else:
                    remove_env_key(key)
            except (PermissionError, ValueError, OSError) as e:
                log.warning("sync_models_to_env: failed to write %s: %s", key, e)

    log.info("Synced model config to .env")


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
    exists = conn.execute(
        "SELECT 1 FROM model_registry WHERE name = ?", (model_name,)
    ).fetchone()
    if not exists:
        conn.close()
        raise ValueError(f"Model '{model_name}' not found in registry")
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
    """Remove DB override for a tier (reverts to .env config)."""
    if tier not in _VALID_TIERS:
        raise ValueError(f"Invalid tier: {tier!r}")
    conn = _connect()
    conn.execute("DELETE FROM tier_assignments WHERE tier = ?", (tier,))
    conn.commit()
    conn.close()
    log.info("Cleared tier assignment for '%s'", tier)


def get_tier_effective_config() -> dict[str, dict]:
    """For each tier, return effective config from DB.

    Returns {tier: {provider, model, base_url, api_key_set, assigned_name}}.
    ``assigned_name`` is the model-registry name if assigned, empty string otherwise.
    """
    assignments = list_tier_assignments()
    result: dict[str, dict] = {}

    for tier in _VALID_TIERS:
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
                }
                continue

        # No DB assignment — tier is unconfigured
        result[tier] = {
            "provider": "", "model": "", "base_url": "",
            "api_key": "", "api_key_set": False, "assigned_name": "",
        }

    return result


# ═══════════════════════════════════════════════════════════════════════════
# System Overrides (heartbeat config via skill_config)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_SKILL_NAME = "_system"

# All admin-configurable system config keys with (type, default_value).
# Default values match config.py. Keys not listed here are not managed via admin portal.
# Excluded keys (internal tuning, not user-facing):
#   PROACTIVE_COOLDOWN_SECONDS, THINK_FALLBACK_MINUTES, LLM_HEARTBEAT_TIMEOUT_SECONDS — internal heartbeat tuning
#   BEDTIME_TIDY_ENABLED, BEDTIME_TIDY_TIMEOUT_S — forced on, not user-configurable
#   PROACTIVE_CHAT_MAX_TOKENS, PROACTIVE_CHAT_HISTORY_TURNS — LLM output tuning
#   BEDTIME_TIDY_MAX_ROUNDS, BEDTIME_TIDY_MAX_TOKENS, BEDTIME_TIDY_TOOLS — internal behavior
#   AWAKE_HOUR_START, AWAKE_HOUR_END — DEPRECATED
#   SLEEP_KEYWORD_HOUR_START/END, SILENCE_SLEEP_AFTER_HOUR, SILENCE_SLEEP_THRESHOLD_HOURS — DEPRECATED
SYSTEM_DEFAULTS: dict[str, tuple[str, any]] = {
    # ── Heartbeat ──
    "HEARTBEAT_INTERVAL_MINUTES":     ("int",   20),
    "MAX_DAILY_PROACTIVE":            ("int",   10),
    "PROACTIVE_COOLDOWN_SECONDS":     ("int",   1800),
    "THINK_FALLBACK_MINUTES":         ("int",   60),
    "LLM_HEARTBEAT_TIMEOUT_SECONDS":  ("int",   120),
    "FALLBACK_WAKE_HOUR":             ("int",   10),
    "BEDTIME_TIDY_ENABLED":           ("bool",  True),
    "BEDTIME_TIDY_TIMEOUT_S":         ("int",   60),
    # ── Sleep/Wake ──
    "SLEEP_KEYWORDS":                 ("str",   "晚安,睡了,去睡了,good night,gn"),
    "SILENCE_PAUSE_DAYS":             ("float", 3.0),
    # ── Basic ──
    "TIMEZONE_OFFSET_HOURS":          ("int",   8),
    "AI_CHAT_MAX_COMPLETION_TOKENS":  ("int",   4096),
    "MAINTENANCE_HOUR":               ("int",   3),
    "MAINTENANCE_ENABLED":            ("bool",  True),
    "HEARTBEAT_LOG_TRIM_DAYS":        ("int",   7),
    "HEARTBEAT_LOG_DELETE_DAYS":      ("int",   30),
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


def normalize_config_value(raw: str, type_name: str) -> str:
    """Normalize a raw config string for comparison across sources.

    Ensures that semantically equal values (e.g. "1"/"true"/"True" for bool)
    compare as identical strings, avoiding spurious sync diffs.
    """
    if type_name == "bool":
        return "True" if raw.lower() in ("true", "1", "yes") else "False"
    if type_name == "int":
        try:
            return str(int(raw))
        except (ValueError, TypeError):
            return raw
    if type_name == "float":
        try:
            return str(float(raw))
        except (ValueError, TypeError):
            return raw
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

    # Unknown key — fallback to config module for backward compat
    log.warning("get_system_config: unknown key %r, falling back to config module", key)
    import mochi.config as _cfg
    return getattr(_cfg, key, None)


def invalidate_system_config_cache() -> None:
    """Force next get_system_config() call to re-read from DB."""
    global _system_config_cache_time
    _system_config_cache_time = 0.0


# ── Seed system config from .env ─────────────────────────────────────────

def seed_system_config_from_env() -> None:
    """Import .env system config into DB on startup, with two-way sync.

    Phase 0 — Remove stale overrides:
      Keys that were previously user-configurable but are now internal-only
      get their DB overrides deleted so the hardcoded defaults take effect.

    Phase 1 — Seed missing keys:
      If DB has no value for a key, seed from .env file or hardcoded default.

    Phase 2 — Sync existing keys:
      If a key is physically present in .env AND differs from DB (after
      normalization), update DB to match .env.  This lets advanced users
      override portal-managed values by editing .env and restarting.

    Reads the raw .env file (not config module attrs) so we can distinguish
    "user wrote this key" from "config.py applied a default".
    """
    from mochi.admin.admin_env import read_env_file

    # Phase 0: clear stale overrides for keys removed from admin UI.
    # These were previously user-configurable but are now internal-only;
    # any leftover DB values (e.g. BEDTIME_TIDY_ENABLED=false) would silently
    # override the intended defaults forever.
    _HIDDEN_KEYS = [
        "PROACTIVE_COOLDOWN_SECONDS",
        "THINK_FALLBACK_MINUTES",
        "LLM_HEARTBEAT_TIMEOUT_SECONDS",
        "BEDTIME_TIDY_ENABLED",
        "BEDTIME_TIDY_TIMEOUT_S",
    ]
    conn = _connect()
    placeholders = ",".join("?" for _ in _HIDDEN_KEYS)
    deleted = conn.execute(
        f"DELETE FROM skill_config WHERE skill_name = ? AND key IN ({placeholders})",
        [_SYSTEM_SKILL_NAME] + _HIDDEN_KEYS,
    ).rowcount
    conn.commit()
    if deleted:
        log.info("Cleared %d stale heartbeat overrides from DB", deleted)
        invalidate_system_config_cache()

    existing = get_system_overrides()
    env_file = read_env_file()  # raw key→value from .env

    # Also need config module for fallback defaults on missing keys
    import mochi.config as cfg

    seeded = synced = 0
    for key in SYSTEM_DEFAULTS:
        type_name, default_val = SYSTEM_DEFAULTS[key]
        env_raw = env_file.get(key)  # None if key not physically in .env

        if key not in existing:
            # Phase 1: seed missing key
            if env_raw is not None:
                set_system_override(key, env_raw)
            else:
                # Fall back to config module (may have non-.env source) or default
                cfg_val = getattr(cfg, key, None)
                if cfg_val is not None:
                    if isinstance(cfg_val, list):
                        cfg_val = ",".join(cfg_val)
                    set_system_override(key, str(cfg_val))
                else:
                    set_system_override(key, str(default_val))
            seeded += 1

        elif env_raw is not None:
            # Phase 2: key exists in both DB and .env — sync if different
            db_str = existing[key]
            norm_env = normalize_config_value(env_raw, type_name)
            norm_db = normalize_config_value(db_str, type_name)
            if norm_env != norm_db:
                set_system_override(key, env_raw)
                synced += 1
                log.info("Synced %s from .env: %r → %r", key, db_str, env_raw)

    if seeded or synced:
        log.info("Config seed: %d new, %d synced from .env", seeded, synced)
    if synced:
        invalidate_system_config_cache()


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


def clear_system_override(key: str) -> None:
    """Remove a system config value (reverts to SYSTEM_DEFAULTS hardcoded default)."""
    conn = _connect()
    conn.execute(
        "DELETE FROM skill_config WHERE skill_name = ? AND key = ?",
        (_SYSTEM_SKILL_NAME, key),
    )
    conn.commit()
    conn.close()
