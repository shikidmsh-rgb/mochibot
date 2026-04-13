"""Tests for mochi/config.py — type casting, logical date, owner persistence, validation."""

import pytest
from datetime import datetime, timezone, timedelta


# ── Type-casting helpers ──

class TestEnvHelpers:
    """Test _env_int, _env_bool, _env_float helper functions."""

    def test_env_int_valid(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        from mochi.config import _env_int
        assert _env_int("TEST_INT", 0) == 42

    def test_env_int_default(self, monkeypatch):
        monkeypatch.delenv("TEST_INT_MISSING", raising=False)
        from mochi.config import _env_int
        assert _env_int("TEST_INT_MISSING", 99) == 99

    def test_env_int_invalid_raises(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_BAD", "abc")
        from mochi.config import _env_int
        with pytest.raises(ValueError):
            _env_int("TEST_INT_BAD", 0)

    def test_env_int_negative(self, monkeypatch):
        monkeypatch.setenv("TEST_INT_NEG", "-5")
        from mochi.config import _env_int
        assert _env_int("TEST_INT_NEG", 0) == -5

    def test_env_bool_true_variants(self, monkeypatch):
        from mochi.config import _env_bool
        for val in ("1", "true", "yes", "True", "YES"):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _env_bool("TEST_BOOL") is True

    def test_env_bool_false_variants(self, monkeypatch):
        from mochi.config import _env_bool
        for val in ("0", "false", "no", "anything", ""):
            monkeypatch.setenv("TEST_BOOL", val)
            assert _env_bool("TEST_BOOL") is False

    def test_env_bool_default_false(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL_MISSING", raising=False)
        from mochi.config import _env_bool
        assert _env_bool("TEST_BOOL_MISSING") is False

    def test_env_float_valid(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "3.14")
        from mochi.config import _env_float
        assert _env_float("TEST_FLOAT", 0.0) == pytest.approx(3.14)

    def test_env_float_default(self, monkeypatch):
        monkeypatch.delenv("TEST_FLOAT_MISSING", raising=False)
        from mochi.config import _env_float
        assert _env_float("TEST_FLOAT_MISSING", 1.5) == pytest.approx(1.5)

    def test_env_returns_string(self, monkeypatch):
        monkeypatch.setenv("TEST_STR", "hello")
        from mochi.config import _env
        assert _env("TEST_STR") == "hello"

    def test_env_default_empty(self, monkeypatch):
        monkeypatch.delenv("TEST_STR_MISSING", raising=False)
        from mochi.config import _env
        assert _env("TEST_STR_MISSING") == ""


# ── Logical date ──

class TestLogicalDate:
    """Test logical_today / logical_yesterday with MAINTENANCE_HOUR rollover."""

    def test_logical_today_before_maintenance(self, monkeypatch):
        """Before MAINTENANCE_HOUR, logical today = yesterday's calendar date."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 6, 15, 2, 0, tzinfo=timezone.utc)
        assert cfg.logical_today(now) == "2025-06-14"

    def test_logical_today_after_maintenance(self, monkeypatch):
        """After MAINTENANCE_HOUR, logical today = today's calendar date."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        assert cfg.logical_today(now) == "2025-06-15"

    def test_logical_today_at_maintenance(self, monkeypatch):
        """At exactly MAINTENANCE_HOUR, logical today = today's calendar date."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 6, 15, 3, 0, tzinfo=timezone.utc)
        assert cfg.logical_today(now) == "2025-06-15"

    def test_logical_yesterday_normal(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        assert cfg.logical_yesterday(now) == "2025-06-14"

    def test_logical_yesterday_before_maintenance(self, monkeypatch):
        """Before MAINTENANCE_HOUR, logical yesterday = two calendar days back."""
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 6, 15, 2, 0, tzinfo=timezone.utc)
        assert cfg.logical_yesterday(now) == "2025-06-13"

    def test_logical_today_month_boundary(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
        now = datetime(2025, 7, 1, 1, 0, tzinfo=timezone.utc)
        assert cfg.logical_today(now) == "2025-06-30"


# ── set_owner_user_id / _persist_owner ──

class TestSetOwnerUserId:

    def test_set_owner_updates_global(self, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "_PROJECT_ROOT", None)
        monkeypatch.setattr(cfg, "_persist_owner", lambda uid: None)
        cfg.set_owner_user_id(42)
        assert cfg.OWNER_USER_ID == 42

    def test_persist_creates_env_file(self, tmp_path, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "_PROJECT_ROOT", tmp_path)
        cfg._persist_owner(123)
        env_path = tmp_path / ".env"
        assert env_path.exists()
        assert "OWNER_USER_ID=123" in env_path.read_text()

    def test_persist_updates_existing(self, tmp_path, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "_PROJECT_ROOT", tmp_path)
        env_path = tmp_path / ".env"
        env_path.write_text("CHAT_MODEL=test\nOWNER_USER_ID=0\nOTHER=x\n")
        cfg._persist_owner(999)
        content = env_path.read_text()
        assert "OWNER_USER_ID=999" in content
        assert "CHAT_MODEL=test" in content
        assert "OTHER=x" in content

    def test_persist_appends_if_missing(self, tmp_path, monkeypatch):
        import mochi.config as cfg
        monkeypatch.setattr(cfg, "_PROJECT_ROOT", tmp_path)
        env_path = tmp_path / ".env"
        env_path.write_text("CHAT_MODEL=test\n")
        cfg._persist_owner(42)
        content = env_path.read_text()
        assert "OWNER_USER_ID=42" in content
        assert "CHAT_MODEL=test" in content


# ── validate_config ──

class TestValidateConfig:

    def _mock_tier_config(self, monkeypatch, *, has_model=True):
        """Make validate_config see a DB-configured model (or not)."""
        def fake_effective():
            if has_model:
                return {"chat": {"model": "m", "api_key_set": True, "source": "db:m"}}
            return {"chat": {"model": "", "api_key_set": False, "source": "none"}}
        monkeypatch.setattr(
            "mochi.admin.admin_db.get_tier_effective_config", fake_effective)

    def test_no_critical_does_not_exit(self, monkeypatch):
        """validate_config should not exit when DB has a model configured."""
        import mochi.config as cfg
        self._mock_tier_config(monkeypatch, has_model=True)
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "some-token")
        cfg.validate_config()

    def test_ollama_skips_api_key_check(self, monkeypatch):
        """validate_config passes when DB has a configured model (e.g. seeded from ollama env)."""
        import mochi.config as cfg
        self._mock_tier_config(monkeypatch, has_model=True)
        monkeypatch.setattr(cfg, "TELEGRAM_BOT_TOKEN", "some-token")
        cfg.validate_config()
