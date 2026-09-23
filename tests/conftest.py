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
def isolated_diary(tmp_path, monkeypatch):
    from mochi.diary import diary
    monkeypatch.setattr(diary, "path", tmp_path / "diary.md")


@pytest.fixture
def extension_state(tmp_path, monkeypatch):
    from mochi.extensions import store

    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    monkeypatch.setattr(skill_registry, "_external_discovered", False)
    monkeypatch.setattr(skill_registry, "_external_errors", {})
    yield
    external = {
        name for name, skill in skill_registry.all_skills().items() if skill.external
    }
    for name in external:
        skill_registry._skills.pop(name, None)
    for tool, owner in list(skill_registry._tool_map.items()):
        if owner in external:
            skill_registry._tool_map.pop(tool)
    skill_registry._capability_summary.clear()


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch, extension_state):
    """Fresh SQLite database for each test."""
    global _skills_discovered
    db_path = tmp_path / "unit_test.db"
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    import mochi.core_store as core_store
    monkeypatch.setattr(core_store, "DATA_DIR", tmp_path / "core_data")
    import mochi.mochi_files_store as files_store
    monkeypatch.setattr(files_store, "DATA_DIR", tmp_path / "files_data")
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
    monkeypatch.setattr(cfg, "MAINTENANCE_HOUR", 3)
    monkeypatch.setattr(cfg, "WEEKLY_MAINTENANCE_ENABLED", True)
    monkeypatch.setattr(cfg, "WEEKLY_MAINTENANCE_MINUTE", 15)
    monkeypatch.setattr(cfg, "TOOL_ROUTER_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_ESCALATION_ENABLED", False)
    monkeypatch.setattr(cfg, "TOOL_LOOP_MAX_ROUNDS", 5)
