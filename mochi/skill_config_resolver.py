"""Skill config resolver — priority chain: DB > env > SKILL.md default.

Pure function module. No global state.
Input: (skill_name, schema from SKILL.md).
Output: dict of resolved, type-cast config values.

Note: MochiBot uses DB > env > schema (admin portal has highest priority).
This differs from upstream Mochi which uses .env > DB > schema.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


# ── Type casting ──────────────────────────────────────────

_CASTERS: dict[str, callable] = {
    "int": int,
    "float": float,
    "bool": lambda v: str(v).lower() in ("true", "1", "yes"),
    "str": str,
}


def _cast(value: str, type_name: str):
    """Cast a string value to the declared type. Raises on failure."""
    caster = _CASTERS.get(type_name, str)
    return caster(value)


def _env_key(skill_name: str, config_key: str) -> str:
    """Build .env variable name: SKILL_{SKILL_UPPER}_{KEY_UPPER}."""
    return f"SKILL_{skill_name.upper()}_{config_key.upper()}"


# ── Main resolver ─────────────────────────────────────────

def resolve_skill_config(skill_name: str, schema: list) -> dict:
    """Resolve all config keys for a skill.

    Priority (highest wins):
      1. DB:          skill_config table (skill_name, key)
      2. .env:        SKILL_{SKILL_UPPER}_{KEY_UPPER}, then bare {KEY}
      3. SKILL.md:    default from schema

    Only processes keys declared in schema. Stale DB rows are ignored.
    Builds and returns a new dict (caller does atomic assignment).
    """
    from mochi.db import get_skill_config

    # Bulk-read all DB overrides in one query
    db_overrides = get_skill_config(skill_name)

    result: dict[str, int | float | bool | str] = {}

    for field in schema:
        key = field.key
        type_name = field.type

        # Priority 1: DB override
        db_val = db_overrides.get(key)
        if db_val is not None:
            try:
                result[key] = _cast(db_val, type_name)
                continue
            except (ValueError, TypeError):
                log.warning(
                    "Skill %s config '%s': bad DB value '%s' for type %s — falling through",
                    skill_name, key, db_val, type_name,
                )

        # Priority 2a: .env override (namespaced: SKILL_HABIT_DIARY_JOURNAL)
        env_name = _env_key(skill_name, key)
        env_val = os.getenv(env_name)
        if env_val is not None:
            try:
                result[key] = _cast(env_val, type_name)
                continue
            except (ValueError, TypeError):
                log.warning(
                    "Skill %s config '%s': bad .env value '%s' for type %s — falling through",
                    skill_name, key, env_val, type_name,
                )

        # Priority 2b: .env override (bare key, backward compat)
        env_val = os.getenv(key)
        if env_val is not None:
            try:
                result[key] = _cast(env_val, type_name)
                continue
            except (ValueError, TypeError):
                log.warning(
                    "Skill %s config '%s': bad .env value '%s' for type %s — falling through",
                    skill_name, key, env_val, type_name,
                )

        # Priority 3: SKILL.md default
        try:
            result[key] = _cast(field.default, type_name)
        except (ValueError, TypeError):
            log.error(
                "Skill %s config '%s': bad default '%s' for type %s — using raw string",
                skill_name, key, field.default, type_name,
            )
            result[key] = field.default

    # Log orphan DB keys at debug level
    schema_keys = {f.key for f in schema}
    orphans = set(db_overrides.keys()) - schema_keys
    if orphans:
        log.debug(
            "Skill %s: ignoring %d orphan DB config key(s): %s",
            skill_name, len(orphans), ", ".join(sorted(orphans)),
        )

    return result
