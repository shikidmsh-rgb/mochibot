"""Settings discovery, real persistence and application boundaries."""

import json
import asyncio
from datetime import datetime, timezone

import pytest

import mochi.db as db
import mochi.skills as registry
from mochi import settings
from mochi.admin import admin_crypto, admin_db
from mochi.skills.base import SkillContext
from mochi.skills.skill_management.handler import SkillManagementSkill


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    admin_db.invalidate_system_config_cache()
    for key in (
        "WEATHER_CITY", "SKILL_WEATHER_WEATHER_CITY",
        "TAVILY_API_KEY", "SKILL_WEB_SEARCH_TAVILY_API_KEY",
        "BAIDU_API_KEY", "SKILL_WEB_SEARCH_BAIDU_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for skill in registry.all_skills().values():
        monkeypatch.setattr(skill, "config", {})
    yield
    admin_db.invalidate_system_config_cache()


async def _call(args, *, authorized=True, source="chat"):
    return await SkillManagementSkill().execute(SkillContext(
        trigger="tool_call", actor="main", source=source,
        owner_authorized=authorized, user_id=1,
        tool_name="manage_settings", args=args,
    ))


def test_catalog_values_share_resolution_and_reset_does_not_mean_empty(monkeypatch):
    monkeypatch.setenv("WEATHER_CITY", "Tokyo")
    setting_id = "skills.weather.config.WEATHER_CITY"
    initial = settings.get_setting(setting_id)
    assert initial["value"] == "Tokyo"
    assert initial["source"] == "environment"
    monkeypatch.setenv("SKILL_WEATHER_WEATHER_CITY", "Shanghai")
    assert settings.get_setting(setting_id)["value"] == "Shanghai"
    saved = settings.change_setting(setting_id, "Osaka")
    assert saved["before"] == "Shanghai" and saved["after"]["value"] == "Osaka"
    assert db.get_skill_config("weather")["WEATHER_CITY"] == "Osaka"
    empty = settings.change_setting(setting_id, "")
    assert empty["after"]["value"] == ""
    assert registry.get_missing_config(registry.get_skill("weather")) == ["WEATHER_CITY"]
    reset = settings.change_setting(setting_id, reset=True)
    assert reset["after"]["value"] == "Shanghai"
    assert reset["after"]["source"] == "environment"
    assert db.get_skill_config("weather") == {}
    listing = settings.list_settings("skills")["items"]
    assert next(item for item in listing if item["id"] == setting_id)["value"] == "Shanghai"


@pytest.mark.asyncio
async def test_city_change_invalidates_observer_without_collection(monkeypatch):
    import mochi.observers as observers
    from mochi.skills.weather.observer import WeatherObserver

    observer = WeatherObserver()
    observer._last_data = {"city": "Tokyo"}
    observer._last_collected_at = datetime.now(timezone.utc)
    observer._consecutive_errors = 5
    observer.meta.enabled = False
    monkeypatch.setattr(observers, "_observers", {"weather": observer})
    settings.change_setting("skills.weather.config.WEATHER_CITY", "Shanghai")
    assert observer._last_data == {}
    assert observer._last_collected_at is None
    assert observer._consecutive_errors == 0
    assert observer.meta.enabled
    started = asyncio.Event()
    finish = asyncio.Event()

    async def collect():
        city = db.get_skill_config("weather")["WEATHER_CITY"]
        started.set()
        await finish.wait()
        return {"city": city}

    monkeypatch.setattr(observer, "observe", collect)
    pending = asyncio.create_task(observer.safe_observe())
    await started.wait()
    settings.change_setting("skills.weather.config.WEATHER_CITY", "Kyoto")
    finish.set()
    await pending
    assert observer._last_data == {}
    assert (await observer.safe_observe())["city"] == "Kyoto"


@pytest.mark.asyncio
async def test_credentials_are_encrypted_and_not_in_receipts_or_arguments(monkeypatch):
    from mochi.tool_execution import outcome_for, serialized_arguments

    monkeypatch.setenv("ADMIN_TOKEN", "settings-test-encryption-root")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    credential = "settings-test-secret"
    setting_id = "skills.web_search.config.BAIDU_API_KEY"
    args = {"action": "set", "id": setting_id, "value": credential}
    result = await _call(args)
    assert result.success and result.state_changed
    stored = db.get_skill_config("web_search")["BAIDU_API_KEY"]
    assert stored != credential and admin_crypto.decrypt_api_key(stored) == credential
    assert json.loads(result.output)["after"]["value"] == {"configured": True}
    assert credential not in result.output
    assert credential not in serialized_arguments("manage_settings", args)
    assert credential not in str(outcome_for("skill_management", "manage_settings", args, result))
    assert not (await _call(args)).state_changed
    monkeypatch.setattr(admin_crypto, "encrypt_api_key", lambda value: value)
    rejected = await _call({**args, "value": "different-test-secret"})
    assert not rejected.success and rejected.error_code == "secret_encryption_failed"
    assert db.get_skill_config("web_search")["BAIDU_API_KEY"] == stored


@pytest.mark.asyncio
async def test_authorization_applies_to_writes_not_autonomous_inspection():
    listed = await _call({"action": "list", "group": "runtime"}, authorized=False, source="runtime:free_time")
    assert listed.success
    args = {"action": "set", "id": "runtime.fallback_wake_hour", "value": "8"}
    denied = await _call(args, authorized=False)
    assert not denied.success and denied.error_code == "user_authorization_required"
    denied = await _call(args, source="runtime:free_time")
    assert not denied.success
    changed = await _call(args)
    assert changed.success and admin_db.get_system_config("FALLBACK_WAKE_HOUR") == 8
    from mochi import heartbeat
    monkey_clock = datetime(2026, 9, 25, 8, tzinfo=timezone.utc)
    old_state, old_changed = heartbeat._state, heartbeat._state_changed_at
    try:
        heartbeat._state = heartbeat.SLEEPING
        heartbeat._state_changed_at = monkey_clock.replace(hour=1)
        assert heartbeat.should_wake_on_schedule(monkey_clock)
    finally:
        heartbeat._state, heartbeat._state_changed_at = old_state, old_changed


def test_runtime_reset_survives_environment_seed_and_validates_awake_window(monkeypatch):
    from mochi.admin import admin_env

    settings.change_setting("runtime.fallback_wake_hour", "8")
    reset = settings.change_setting("runtime.fallback_wake_hour", reset=True)
    assert reset["after"]["value"] == 10
    monkeypatch.setattr(admin_env, "read_env_file", lambda: {"FALLBACK_WAKE_HOUR": "7"})
    admin_db.seed_system_config_from_env()
    assert admin_db.get_system_config("FALLBACK_WAKE_HOUR") == 10
    with pytest.raises(settings.SettingsError) as rejected:
        settings.change_setting("runtime.sleep_after_hour", "9")
    assert rejected.value.code == "invalid_awake_window"
    assert admin_db.get_system_config("SLEEP_AFTER_HOUR") == 23


def test_extension_draft_config_does_not_import_code_or_collide_with_switch(extension_state):
    from mochi.extensions import store

    package = store.ROOT / "local_settings" / "draft"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: local_settings\nmod_api: 1\ntype: tool\nconfig:\n"
        "  enabled:\n    type: bool\n    default: false\n"
        "    description: Application flag\n---\n\n## Tools\n\n"
        "### local_settings_read (on_demand)\nRead data.\n\n"
        "| Parameter | Type | Required | Description |\n"
        "|-----------|------|----------|-------------|\n",
        encoding="utf-8",
    )
    (package / "handler.py").write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    db.set_skill_enabled("local_settings", False)
    changed = settings.change_setting("skills.local_settings.config.enabled", "true")
    assert changed["after"]["value"] is True
    assert changed["after"]["draft_only"]
    assert settings.get_setting("skills.local_settings.enabled")["value"] is False
    assert registry.get_skill("local_settings") is None
    with pytest.raises(settings.SettingsError):
        settings.change_setting("skills.local_settings.config.enabled", "perhaps")
    assert db.get_skill_config("local_settings")["enabled"] == "true"


def test_model_inspection_never_initializes_pool_and_reports_saved_separately(monkeypatch):
    import mochi.model_pool as pool
    from mochi.admin import admin_env

    monkeypatch.setattr(pool, "_pool", None)
    monkeypatch.setattr(admin_env, "read_env_value", lambda key: {
        "EMBEDDING_PROVIDER": "openai", "EMBEDDING_MODEL": "test-embedding",
        "EMBEDDING_API_KEY": "embedding-test-secret",
    }.get(key))
    result = settings.get_setting("models.embedding")
    assert result["value"]["saved"]["configured"]
    assert result["value"]["running"] is None
    assert pool._pool is None
    assert "embedding-test-secret" not in str(result)
    with pytest.raises(settings.SettingsError) as rejected:
        settings.change_setting("models.embedding", "changed")
    assert rejected.value.code == "read_only_setting"


def test_settings_schema_and_pure_chat_share_the_same_callable_entry():
    from mochi.tool_availability import ToolAvailability
    from mochi.turn_tool_policy import build_turn_tool_plan

    skill = registry.get_skill("skill_management")
    definitions = skill.get_tools()
    assert len(definitions) == 1
    availability = ToolAvailability.from_definitions(definitions, source="test")
    assert availability.allows("manage_settings")
    assert availability.validate_arguments("manage_settings", {
        "action": "set", "id": "runtime.sleep_after_hour", "value": 23,
    })
    db.set_skill_mode("off")
    plan = build_turn_tool_plan()
    assert "manage_settings" in {
        definition["function"]["name"] for definition in plan.resident_definitions
    }


def test_secret_and_required_are_independent_in_admin_and_share_writes(monkeypatch):
    from fastapi.testclient import TestClient
    from mochi.admin.admin_server import app
    import mochi.config as config

    monkeypatch.setattr(config, "ADMIN_TOKEN", "settings-admin-test")
    monkeypatch.setenv("ADMIN_TOKEN", "settings-admin-test")
    monkeypatch.setattr(admin_crypto, "_fernet_instance", None)
    client = TestClient(app, headers={"Authorization": "Bearer settings-admin-test"})
    response = client.put("/api/skills/weather/config", json={"key": "WEATHER_CITY", "value": "Kyoto"})
    assert response.status_code == 200
    assert settings.get_setting("skills.weather.config.WEATHER_CITY")["value"] == "Kyoto"
    response = client.put("/api/skills/web_search/config", json={"key": "BAIDU_API_KEY", "value": "admin-test-secret"})
    assert response.status_code == 200
    assert admin_crypto.is_encrypted(db.get_skill_config("web_search")["BAIDU_API_KEY"])
    response = client.put("/api/preferences", json={"MAX_DAILY_PROACTIVE": 11})
    assert response.status_code == 400
    assert admin_db.get_system_config("MAX_DAILY_PROACTIVE") == 5
