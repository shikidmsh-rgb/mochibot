"""Shared fixtures for unit tests.

Provides:
- fresh_db: isolated SQLite database per test
- mock_config: override config values so tests don't need .env
"""

import pytest
from datetime import timezone, timedelta

from mochi.db import init_db
import mochi.skills as skill_registry


UTC = timezone.utc

# Ensure skills are discovered once (module-level state)
_skills_discovered = False


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh SQLite database for each test."""
    global _skills_discovered
    db_path = tmp_path / "unit_test.db"
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()
    if not _skills_discovered:
        skill_registry.discover()
        _skills_discovered = True
    skill_registry.init_all_skill_schemas()
    yield db_path


@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Override config values so unit tests never rely on .env."""
    import mochi.config as cfg
    monkeypatch.setattr(cfg, "OWNER_USER_ID", 1)
    monkeypatch.setattr(cfg, "TIMEZONE_OFFSET_HOURS", 0)
    monkeypatch.setattr(cfg, "TZ", UTC)
    # Also patch TZ in modules that imported it at module level
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "TZ", UTC)
    # Patch observer module-level TZ too
    import mochi.observers.activity_pattern.observer as ap_obs
    monkeypatch.setattr(ap_obs, "TZ", UTC)
    monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 5)
