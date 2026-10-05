"""Shared isolation for the ten deterministic workflow checks."""

import asyncio
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    from mochi import config, core_store, db, heartbeat, heartbeat_runtime, mochi_files_store, skills
    from mochi import reminder_timer, tool_policy, turn_tool_policy
    from mochi.admin import admin_db
    from mochi.diary import diary
    from mochi.extensions import store

    for key, value in {
        "OWNER_USER_ID": 1, "TZ": timezone.utc, "TIMEZONE_OFFSET_HOURS": 0,
        "TOOL_ROUTER_ENABLED": False, "TOOL_ESCALATION_ENABLED": True,
        "TOOL_LOOP_MAX_ROUNDS": 5, "FREE_TIME_ENABLED": True,
        "BEDTIME_ENTRY_ENABLED": True, "WEEKLY_MAINTENANCE_ENABLED": True,
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "mochi.db")
    monkeypatch.setattr(db, "TZ", timezone.utc)
    monkeypatch.setattr(heartbeat_runtime, "TZ", timezone.utc)
    monkeypatch.setattr(core_store, "DATA_DIR", tmp_path / "core")
    monkeypatch.setattr(mochi_files_store, "DATA_DIR", tmp_path / "documents")
    monkeypatch.setattr(diary, "path", tmp_path / "diary.md")
    monkeypatch.setattr(store, "ROOT", tmp_path / "extensions")
    monkeypatch.setattr(skills, "_external_discovered", False)
    monkeypatch.setattr(skills, "_external_errors", {})
    monkeypatch.setattr(admin_db, "_system_config_cache", {})
    monkeypatch.setattr(admin_db, "_system_config_cache_time", 0)
    monkeypatch.setattr(turn_tool_policy, "_session_toolboxes", {})
    monkeypatch.setattr(tool_policy, "_deny_set", set())
    monkeypatch.setattr(tool_policy, "_call_log", {})
    for key, value in {
        "_state": heartbeat.AWAKE, "TZ": timezone.utc,
        "_STATE_FILE": tmp_path / "heartbeat.json",
        "_state_changed_at": datetime.now(timezone.utc),
        "_silent_pause": False, "_last_sleep_at": None,
        "_active_chat_tokens": set(), "_chat_activity_generation": 0,
        "_runtime_prepare_callback": None, "_runtime_delivery_callback": None,
        "_runtime_transport": "", "_dream_callback": None,
    }.items():
        monkeypatch.setattr(heartbeat, key, value)
    for key, value in {
        "_send_callback": None, "_self_prepare_callback": None,
        "_self_delivery_callback": None, "_self_transport": "",
        "_self_reminder_lock": asyncio.Lock(), "_heap": [],
        "_heap_event": None, "_active_ids": set(),
    }.items():
        monkeypatch.setattr(reminder_timer, key, value)
    db.init_db()
    if not skills.all_skills():
        skills.discover()
    skills.init_all_skill_schemas()
    yield
    external = {name for name, skill in skills.all_skills().items() if skill.external}
    for name in external:
        skills._skills.pop(name, None)
    for tool, owner in list(skills._tool_map.items()):
        if owner in external:
            skills._tool_map.pop(tool)


@pytest.fixture
def mock_llm_factory(monkeypatch):
    from mochi import ai_client
    from tests.mock_llm import MockLLMProvider

    monkeypatch.setattr(ai_client, "_retrieve_memories_for_turn", lambda *args: [])
    monkeypatch.setattr(ai_client, "_schedule_continuous_memory", lambda _user: None)

    def factory(responses):
        client = MockLLMProvider(responses)
        monkeypatch.setattr(ai_client, "get_client_for_tier", lambda _tier: client)
        return client

    return factory
