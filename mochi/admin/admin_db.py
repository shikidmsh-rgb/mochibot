"""Admin portal — DB helpers for model registry, tier assignments, system overrides."""

import logging
from datetime import datetime, timezone, timedelta

from mochi.config import (
    DB_PATH, TIMEZONE_OFFSET_HOURS,
    CHAT_PROVIDER, CHAT_API_KEY, CHAT_MODEL, CHAT_BASE_URL,
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
    log.info("Cleared tier assignment for '%s' (reverted to .env)", tier)


def get_tier_effective_config() -> dict[str, dict]:
    """For each tier, return effective config: DB if assigned, .env fallback otherwise.

    Returns {tier: {provider, model, base_url, api_key_set, source}}.
    """
    from mochi.config import (
        TIER_LITE_PROVIDER, TIER_LITE_API_KEY, TIER_LITE_MODEL, TIER_LITE_BASE_URL,
        TIER_CHAT_PROVIDER, TIER_CHAT_API_KEY, TIER_CHAT_MODEL, TIER_CHAT_BASE_URL,
        TIER_DEEP_PROVIDER, TIER_DEEP_API_KEY, TIER_DEEP_MODEL, TIER_DEEP_BASE_URL,
    )
    env_tiers = {
        "lite":    (TIER_LITE_PROVIDER, TIER_LITE_API_KEY, TIER_LITE_MODEL, TIER_LITE_BASE_URL),
        "chat":    (TIER_CHAT_PROVIDER, TIER_CHAT_API_KEY, TIER_CHAT_MODEL, TIER_CHAT_BASE_URL),
        "deep":    (TIER_DEEP_PROVIDER, TIER_DEEP_API_KEY, TIER_DEEP_MODEL, TIER_DEEP_BASE_URL),
    }

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
                    "source": f"db:{assignments[tier]}",
                }
                continue

        # Env fallback (with CHAT_* as final fallback)
        provider, api_key, model, base_url = env_tiers.get(tier, ("", "", "", ""))
        eff_provider = provider or CHAT_PROVIDER
        eff_model = model or CHAT_MODEL
        eff_base_url = base_url or CHAT_BASE_URL
        eff_key_set = bool(api_key or CHAT_API_KEY)

        result[tier] = {
            "provider": eff_provider,
            "model": eff_model,
            "base_url": eff_base_url,
            "api_key_set": eff_key_set,
            "source": "env",
        }

    return result


# ═══════════════════════════════════════════════════════════════════════════
# System Overrides (heartbeat config via skill_config)
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_SKILL_NAME = "_system"


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
    """Remove a system override (revert to .env default)."""
    conn = _connect()
    conn.execute(
        "DELETE FROM skill_config WHERE skill_name = ? AND key = ?",
        (_SYSTEM_SKILL_NAME, key),
    )
    conn.commit()
    conn.close()
