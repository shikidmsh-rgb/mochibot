"""Tests for mochi/skill_config_resolver.py — priority chain, type casting."""

import pytest
from unittest.mock import patch
from dataclasses import dataclass


@dataclass
class ConfigField:
    """Minimal ConfigField for testing (mirrors mochi.skills.base.ConfigField)."""
    key: str
    type: str
    default: str
    description: str = ""


class TestCast:

    def test_cast_int(self):
        from mochi.skill_config_resolver import _cast
        assert _cast("42", "int") == 42

    def test_cast_float(self):
        from mochi.skill_config_resolver import _cast
        assert _cast("3.14", "float") == pytest.approx(3.14)

    def test_cast_bool_true(self):
        from mochi.skill_config_resolver import _cast
        for val in ("true", "1", "yes"):
            assert _cast(val, "bool") is True

    def test_cast_bool_false(self):
        from mochi.skill_config_resolver import _cast
        for val in ("false", "0", "no", "anything"):
            assert _cast(val, "bool") is False

    def test_cast_str(self):
        from mochi.skill_config_resolver import _cast
        assert _cast("hello", "str") == "hello"

    def test_cast_invalid_int_raises(self):
        from mochi.skill_config_resolver import _cast
        with pytest.raises(ValueError):
            _cast("abc", "int")

    def test_cast_unknown_type_defaults_to_str(self):
        from mochi.skill_config_resolver import _cast
        assert _cast("something", "unknown_type") == "something"


class TestEnvKey:

    def test_formatting(self):
        from mochi.skill_config_resolver import _env_key
        assert _env_key("habit", "journal") == "SKILL_HABIT_JOURNAL"

    def test_formatting_uppercase(self):
        from mochi.skill_config_resolver import _env_key
        assert _env_key("web_search", "max_results") == "SKILL_WEB_SEARCH_MAX_RESULTS"


class TestResolveSkillConfig:

    def _make_schema(self, *fields):
        """Build a list of ConfigField objects."""
        return [ConfigField(key=k, type=t, default=d) for k, t, d in fields]

    @patch("mochi.db.get_skill_config")
    def test_db_wins_over_env_and_default(self, mock_db, monkeypatch):
        mock_db.return_value = {"max_results": "20"}
        monkeypatch.setenv("SKILL_SEARCH_MAX_RESULTS", "50")
        schema = self._make_schema(("max_results", "int", "10"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("search", schema)
        assert result["max_results"] == 20

    @patch("mochi.db.get_skill_config")
    def test_env_namespaced_wins_over_default(self, mock_db, monkeypatch):
        mock_db.return_value = {}
        monkeypatch.setenv("SKILL_HABIT_ENABLED", "true")
        schema = self._make_schema(("enabled", "bool", "false"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("habit", schema)
        assert result["enabled"] is True

    @patch("mochi.db.get_skill_config")
    def test_env_bare_key_wins_over_default(self, mock_db, monkeypatch):
        mock_db.return_value = {}
        monkeypatch.delenv("SKILL_WEATHER_CITY", raising=False)
        monkeypatch.setenv("CITY", "Tokyo")
        schema = self._make_schema(("CITY", "str", "NYC"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("weather", schema)
        assert result["CITY"] == "Tokyo"

    @patch("mochi.db.get_skill_config")
    def test_default_when_nothing_set(self, mock_db, monkeypatch):
        mock_db.return_value = {}
        monkeypatch.delenv("SKILL_HABIT_LIMIT", raising=False)
        monkeypatch.delenv("LIMIT", raising=False)
        schema = self._make_schema(("LIMIT", "int", "5"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("habit", schema)
        assert result["LIMIT"] == 5

    @patch("mochi.db.get_skill_config")
    def test_bad_db_falls_through_to_env(self, mock_db, monkeypatch):
        mock_db.return_value = {"count": "not_a_number"}
        monkeypatch.setenv("SKILL_TEST_COUNT", "7")
        schema = self._make_schema(("count", "int", "3"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("test", schema)
        assert result["count"] == 7

    @patch("mochi.db.get_skill_config")
    def test_bad_env_falls_through_to_default(self, mock_db, monkeypatch):
        mock_db.return_value = {}
        monkeypatch.setenv("SKILL_TEST_COUNT", "bad")
        monkeypatch.delenv("COUNT", raising=False)
        schema = self._make_schema(("COUNT", "int", "3"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("test", schema)
        assert result["COUNT"] == 3

    @patch("mochi.db.get_skill_config")
    def test_orphan_db_keys_ignored(self, mock_db, monkeypatch):
        mock_db.return_value = {"stale_key": "value", "name": "bot"}
        schema = self._make_schema(("name", "str", "default"))

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("test", schema)
        assert "stale_key" not in result
        assert result["name"] == "bot"

    @patch("mochi.db.get_skill_config")
    def test_empty_schema_returns_empty(self, mock_db):
        mock_db.return_value = {}

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("test", [])
        assert result == {}

    @patch("mochi.db.get_skill_config")
    def test_multiple_fields_mixed_sources(self, mock_db, monkeypatch):
        mock_db.return_value = {"a": "10"}
        monkeypatch.setenv("SKILL_MIX_B", "hello")
        monkeypatch.delenv("SKILL_MIX_C", raising=False)
        monkeypatch.delenv("C", raising=False)
        schema = self._make_schema(
            ("a", "int", "1"),
            ("b", "str", "default"),
            ("c", "bool", "true"),
        )

        from mochi.skill_config_resolver import resolve_skill_config
        result = resolve_skill_config("mix", schema)
        assert result["a"] == 10      # from DB
        assert result["b"] == "hello"  # from env
        assert result["c"] is True     # from default
