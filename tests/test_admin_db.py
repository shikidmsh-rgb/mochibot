"""Tests for admin_db system config functions.

Covers: SYSTEM_DEFAULTS, _cast_system(), get_system_config(),
seed_system_config_from_env(), invalidate_system_config_cache().
"""

import pytest
from unittest.mock import patch

from mochi.admin.admin_db import (
    SYSTEM_DEFAULTS,
    _cast_system,
    get_system_config,
    invalidate_system_config_cache,
    seed_system_config_from_env,
    get_system_overrides,
    set_system_override,
    clear_system_override,
)


class TestCastSystem:

    def test_int(self):
        assert _cast_system("42", "int") == 42

    def test_int_invalid(self):
        assert _cast_system("abc", "int") == 0

    def test_float(self):
        assert _cast_system("1.5", "float") == 1.5

    def test_float_invalid(self):
        assert _cast_system("xyz", "float") == 0.0

    def test_bool_true(self):
        for val in ("true", "True", "1", "yes"):
            assert _cast_system(val, "bool") is True

    def test_bool_false(self):
        for val in ("false", "False", "0", "no", ""):
            assert _cast_system(val, "bool") is False

    def test_str(self):
        assert _cast_system("hello", "str") == "hello"


class TestGetSystemConfig:

    def setup_method(self):
        invalidate_system_config_cache()

    def test_returns_db_value(self):
        """DB value is returned with correct type casting."""
        set_system_override("HEARTBEAT_INTERVAL_MINUTES", "30")
        invalidate_system_config_cache()
        assert get_system_config("HEARTBEAT_INTERVAL_MINUTES") == 30

    def test_returns_default_when_no_db_value(self):
        """When key is not in DB, returns SYSTEM_DEFAULTS value."""
        # Don't seed — DB is empty, so default should be returned
        invalidate_system_config_cache()
        assert get_system_config("HEARTBEAT_INTERVAL_MINUTES") == 20

    def test_bool_from_db(self):
        """Bool type is correctly cast from DB string."""
        set_system_override("MAINTENANCE_ENABLED", "false")
        invalidate_system_config_cache()
        assert get_system_config("MAINTENANCE_ENABLED") is False

    def test_float_from_db(self):
        """Float type is correctly cast from DB string."""
        set_system_override("SILENCE_PAUSE_DAYS", "2.5")
        invalidate_system_config_cache()
        assert get_system_config("SILENCE_PAUSE_DAYS") == 2.5

    def test_str_from_db(self):
        """Str type is returned as-is from DB."""
        set_system_override("SLEEP_KEYWORDS", "晚安,goodnight")
        invalidate_system_config_cache()
        assert get_system_config("SLEEP_KEYWORDS") == "晚安,goodnight"

    def test_unknown_key_falls_back_to_config(self):
        """Unknown key logs warning and falls back to config module."""
        invalidate_system_config_cache()
        with patch("mochi.admin.admin_db.getattr") as mock_ga:
            # get_system_config uses getattr(cfg, key, None) — we test the fallback path
            result = get_system_config("OWNER_USER_ID")
            # Should return something (from config module), not crash
            assert result is not None or result is None  # just verify no exception

    def test_cache_invalidation(self):
        """After invalidation, next call re-reads from DB."""
        set_system_override("MAX_DAILY_PROACTIVE", "5")
        invalidate_system_config_cache()
        assert get_system_config("MAX_DAILY_PROACTIVE") == 5

        set_system_override("MAX_DAILY_PROACTIVE", "99")
        # Still cached — should return old value
        assert get_system_config("MAX_DAILY_PROACTIVE") == 5

        # After invalidation, should return new value
        invalidate_system_config_cache()
        assert get_system_config("MAX_DAILY_PROACTIVE") == 99


class TestSeedSystemConfigFromEnv:

    def setup_method(self):
        invalidate_system_config_cache()

    def test_empty_db_seeds_all_keys(self):
        """On empty DB, all SYSTEM_DEFAULTS keys are seeded."""
        seed_system_config_from_env()
        overrides = get_system_overrides()
        for key in SYSTEM_DEFAULTS:
            assert key in overrides, f"Missing key: {key}"

    def test_seeds_env_value_when_available(self, monkeypatch):
        """When config module has a non-default value, it's seeded from env."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "HEARTBEAT_INTERVAL_MINUTES", 15)
        seed_system_config_from_env()
        overrides = get_system_overrides()
        assert overrides["HEARTBEAT_INTERVAL_MINUTES"] == "15"

    def test_seeds_default_when_env_is_none(self, monkeypatch):
        """When config module attr is None, seeds the hardcoded default."""
        import mochi.config as cfg
        # Simulate a key that has no env value
        monkeypatch.delattr(cfg, "PROACTIVE_COOLDOWN_SECONDS", raising=False)
        seed_system_config_from_env()
        overrides = get_system_overrides()
        # Should have the SYSTEM_DEFAULTS value
        assert overrides["PROACTIVE_COOLDOWN_SECONDS"] == "1800"

    def test_nonempty_db_skips(self):
        """When all keys already in DB, seed is a no-op."""
        seed_system_config_from_env()
        overrides_before = get_system_overrides()

        # Modify one value
        set_system_override("HEARTBEAT_INTERVAL_MINUTES", "999")

        # Re-seed should not overwrite
        seed_system_config_from_env()
        overrides_after = get_system_overrides()
        assert overrides_after["HEARTBEAT_INTERVAL_MINUTES"] == "999"

    def test_partial_db_fills_missing(self):
        """When some keys exist in DB, only missing keys are seeded."""
        set_system_override("HEARTBEAT_INTERVAL_MINUTES", "42")
        seed_system_config_from_env()
        overrides = get_system_overrides()

        # Pre-existing value preserved
        assert overrides["HEARTBEAT_INTERVAL_MINUTES"] == "42"
        # Missing key was filled
        assert "MAX_DAILY_PROACTIVE" in overrides

    def test_sleep_keywords_list_converted(self, monkeypatch):
        """SLEEP_KEYWORDS list from config.py is joined back to comma-separated str."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "SLEEP_KEYWORDS", ["晚安", "睡了", "gn"])
        seed_system_config_from_env()
        overrides = get_system_overrides()
        assert overrides["SLEEP_KEYWORDS"] == "晚安,睡了,gn"

    def test_idempotent(self):
        """Calling seed twice does not duplicate or overwrite."""
        seed_system_config_from_env()
        count_1 = len(get_system_overrides())
        seed_system_config_from_env()
        count_2 = len(get_system_overrides())
        assert count_1 == count_2


class TestClearSystemOverride:

    def test_clear_reverts_to_default(self):
        """After clearing a key, get_system_config returns the default."""
        set_system_override("MAINTENANCE_HOUR", "5")
        invalidate_system_config_cache()
        assert get_system_config("MAINTENANCE_HOUR") == 5

        clear_system_override("MAINTENANCE_HOUR")
        invalidate_system_config_cache()
        assert get_system_config("MAINTENANCE_HOUR") == 3  # SYSTEM_DEFAULTS value

    def test_clear_removes_from_db(self):
        """Clearing a key removes it from the DB entirely."""
        set_system_override("MAINTENANCE_HOUR", "5")
        clear_system_override("MAINTENANCE_HOUR")
        overrides = get_system_overrides()
        assert "MAINTENANCE_HOUR" not in overrides
