"""Opt-in visibility changes preserve Main agency and execution boundaries."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

import mochi.db as db
import mochi.skills as registry
from mochi.adaptive_tool_load import pin_definition, recalculate, resolve_definition
from mochi.skills.base import SkillContext
from mochi.skills.skill_management.handler import SkillManagementSkill
from mochi.tool_availability import ToolAvailability

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _definition():
    return {
        "type": "function", "_load": "on_demand", "_adaptive_load": True,
        "function": {
            "name": "search_personal_history", "description": "Search",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _used(turn, *, source="chat", status="success", user_id=1, days_ago=0):
    execution = db.start_tool_execution(
        turn_id=turn, tool_call_id=f"call-{turn}", user_id=user_id,
        source=source, skill_name="internal_search",
        tool_name="search_personal_history", action="", arguments_json="{}",
    )
    if status != "running":
        db.finish_tool_execution(execution, status=status)
    with db._connect() as conn:
        conn.execute(
            "UPDATE tool_executions SET started_at=? WHERE id=?",
            ((NOW - timedelta(days=days_ago)).isoformat(), execution),
        )


def test_preloaded_batch_state_never_rereads_sqlite(monkeypatch):
    def unexpected_read():
        pytest.fail("preloaded batch state must not cause another SQLite read")

    monkeypatch.setattr(db, "get_adaptive_tool_load_states", unexpected_read)
    definition = _definition()
    for states, expected in (
        ({}, "on_demand"),
        ({"search_personal_history": {"effective_load": "routed"}}, "routed"),
    ):
        resolved = [resolve_definition(definition, states=states) for _ in range(40)]
        assert all(tool["_load"] == expected for tool in resolved)
    assert definition["_load"] == "on_demand"


def test_distinct_successful_owner_chat_turns_drive_adaptation():
    definition = _definition()
    for _ in range(3):
        _used("same-turn")
    for source in ("runtime:free_time", "runtime:self_reminder", "runtime:bedtime"):
        _used(source, source=source)
    _used("failed", status="failed")
    _used("running", status="running")
    _used("other-user", user_id=2)
    _used("old", days_ago=31)
    initial = recalculate([definition], user_id=1, now=NOW)["search_personal_history"]
    assert initial["used_turns"] == 1 and initial["effective_load"] == "on_demand"
    _used("second")
    _used("third")
    promoted = recalculate([definition], user_id=1, now=NOW)["search_personal_history"]
    assert promoted["effective_load"] == "routed" and promoted["changed"]
    raw = deepcopy(definition)
    assert resolve_definition(definition)["_load"] == "routed"
    assert definition == raw
    held = recalculate([definition], user_id=1, now=NOW + timedelta(days=6))
    assert held["search_personal_history"]["effective_load"] == "routed"
    reverted = recalculate([definition], user_id=1, now=NOW + timedelta(days=31))
    assert reverted["search_personal_history"]["effective_load"] == "on_demand"


def test_pin_reset_recalculates_from_default_without_sticking_to_pin():
    definition = _definition()
    pinned = pin_definition(definition, "routed", user_id=1, now=NOW)
    assert pinned["changed"] and pinned["effective_load"] == "routed"
    maintained = recalculate([definition], user_id=1, now=NOW + timedelta(days=40))
    assert maintained["search_personal_history"]["effective_load"] == "routed"
    reset = pin_definition(definition, None, user_id=1, now=NOW + timedelta(days=40))
    assert reset["changed"] and reset["effective_load"] == "on_demand"
    for turn in ("one", "two", "three"):
        _used(turn)
    pin_definition(definition, "on_demand", user_id=1, now=NOW)
    reset = pin_definition(definition, None, user_id=1, now=NOW)
    assert reset["effective_load"] == "routed" and reset["pinned_load"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat", "runtime:self_reminder", "runtime:free_time", "runtime:bedtime"])
async def test_main_can_manage_visibility_without_owner_chat_gate(source):
    result = await SkillManagementSkill().execute(SkillContext(
        trigger="tool_call", actor="main", source=source, user_id=1,
        owner_authorized=False, tool_name="manage_tool_load",
        args={"action": "pin", "tool_name": "search_personal_history", "load": "routed"},
    ))
    assert result.success and result.state_changed
    assert "pinned by Main" in result.output


@pytest.mark.asyncio
async def test_scripts_fixed_contracts_and_invalid_shapes_stay_rejected():
    skill = registry.get_skill("skill_management")
    unavailable = await skill.execute(SkillContext(
        trigger="script", actor="main", tool_name="manage_tool_load",
        args={"action": "pin", "tool_name": "search_personal_history", "load": "routed"},
    ))
    assert not unavailable.success and unavailable.error_code == "main_required"
    fixed = await skill.execute(SkillContext(
        trigger="tool_call", actor="main", tool_name="manage_tool_load",
        args={"action": "pin", "tool_name": "update_core", "load": "routed"},
    ))
    assert not fixed.success and fixed.error_code == "fixed_tool_load"
    definitions = [tool for tool in skill.get_tools() if tool["function"]["name"] == "manage_tool_load"]
    availability = ToolAvailability.from_definitions(definitions, source="test")
    assert availability.validate_arguments("manage_tool_load", {
        "action": "pin", "tool_name": "search_personal_history",
    })
    assert availability.validate_arguments("manage_tool_load", {
        "action": "reset", "tool_name": "search_personal_history", "load": "routed",
    })
    assert availability.validate_arguments("manage_tool_load", {
        "action": "pin", "tool_name": "search_personal_history", "load": "resident",
    })


@pytest.mark.asyncio
async def test_daily_free_time_keeps_persisted_key_and_checks_ten():
    from mochi.admin.admin_db import get_system_config, set_system_override

    set_system_override("MAX_DAILY_PROACTIVE", "7")
    skill = SkillManagementSkill()
    viewed = skill._get_agent_settings()
    assert "max_daily_proactive = 7 (范围 0–10)" in viewed.output
    assert "Attention" not in viewed.output
    for value in (11, -1, 1.5):
        assert not skill._set_agent_setting("max_daily_proactive", value).success
        assert get_system_config("MAX_DAILY_PROACTIVE") == 7
    assert skill._set_agent_setting("max_daily_proactive", 0).success
    assert get_system_config("MAX_DAILY_PROACTIVE") == 0
    assert skill._set_agent_setting("max_daily_proactive", 10).success
