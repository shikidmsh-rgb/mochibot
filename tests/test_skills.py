"""Tests for skill registry and SKILL.md parsing."""

import os
import tempfile

import pytest

# Isolated DB for tests that hit get_tools / get_disabled_skills
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db.close()
os.environ["MOCHIBOT_DB_PATH"] = _temp_db.name

from mochi.db import init_db
init_db()

from mochi.skills.base import _parse_skill_md, _parse_config_schema, Skill, SkillContext, SkillResult


class TestSkillMdParser:
    def test_parse_nonexistent(self):
        result = _parse_skill_md("/nonexistent/SKILL.md")
        assert result["expose_as_tool"] is True
        assert result["triggers"] == ["tool_call"]
        assert result["tools"] == []

    def test_parse_real_skill(self, tmp_path):
        """v1 format: ## Tool: tool_name"""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: test_skill
expose: true
triggers: [tool_call]
---

## Tool: do_thing
Description: Does the thing

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| action | string | yes | what to do |
| count | integer | no | how many times |
""")
        result = _parse_skill_md(str(md))
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "do_thing"
        assert tool["function"]["description"] == "Does the thing"
        params = tool["function"]["parameters"]
        assert "action" in params["properties"]
        assert "count" in params["properties"]
        assert "action" in params["required"]
        assert "count" not in params["required"]

    def test_parse_multi_tool(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: multi
expose: true
triggers: [tool_call, cron]
---

## Tool: tool_a
Description: First tool

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| x | string | yes | param x |

## Tool: tool_b
Description: Second tool

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| y | integer | no | param y |
""")
        result = _parse_skill_md(str(md))
        assert len(result["tools"]) == 2
        assert result["triggers"] == ["tool_call", "cron"]
        names = [t["function"]["name"] for t in result["tools"]]
        assert "tool_a" in names
        assert "tool_b" in names

    def test_expose_false(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: hidden
expose: false
---
""")
        result = _parse_skill_md(str(md))
        assert result["expose_as_tool"] is False


class TestSkillMdParserV2:
    """Tests for v2 SKILL.md format."""

    def test_parse_sense_field(self, tmp_path):
        """sense: block should set has_sense=True."""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: sensor_skill
sense:
  interval: 30
---
""")
        result = _parse_skill_md(str(md))
        assert result["has_sense"] is True

    def test_parse_sense_false_by_default(self, tmp_path):
        """Skills without sense: should default to False."""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: no_sense
---
""")
        result = _parse_skill_md(str(md))
        assert result["has_sense"] is False

    def test_parse_diary_tags(self, tmp_path):
        """diary: [journal, today_ctx] should be parsed as list."""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: diary_skill
diary: [journal, today_ctx]
---
""")
        result = _parse_skill_md(str(md))
        assert result["diary"] == ["journal", "today_ctx"]

    def test_parse_diary_empty_by_default(self, tmp_path):
        """Skills without diary: should default to []."""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: no_diary
---
""")
        result = _parse_skill_md(str(md))
        assert result["diary"] == []

    def test_parse_v2_format(self, tmp_path):
        """v2 format: ## Tools / ### tool_name"""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: v2skill
description: A v2 skill
type: tool
expose_as_tool: true
multi_turn: true
---

## Tools

### do_thing
Does the thing with v2 format

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string | yes | what to do |
| count | integer | no | how many times |

### do_other
Does something else

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| target | string | yes | target name |
""")
        result = _parse_skill_md(str(md))
        assert result["expose_as_tool"] is True
        assert result["multi_turn"] is True
        assert result["type"] == "tool"
        assert result["meta"]["name"] == "v2skill"
        assert result["meta"]["description"] == "A v2 skill"
        assert len(result["tools"]) == 2
        names = [t["function"]["name"] for t in result["tools"]]
        assert "do_thing" in names
        assert "do_other" in names
        # Check params
        do_thing = [t for t in result["tools"] if t["function"]["name"] == "do_thing"][0]
        assert "action" in do_thing["function"]["parameters"]["properties"]
        assert "action" in do_thing["function"]["parameters"]["required"]

    def test_extract_usage_rules(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: rules_test
expose_as_tool: true
---

## Tools

### my_tool
A tool

## Usage Rules
- Always call with action=check first
- Never call more than 3 times per conversation

## Behavior Rules
- Be polite in responses
""")
        result = _parse_skill_md(str(md))
        assert "Always call with action=check first" in result["usage_rules"]
        assert "Be polite in responses" in result["usage_rules"]

    def test_expose_as_tool_false_v2(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: bg_task
expose_as_tool: false
type: automation
---
""")
        result = _parse_skill_md(str(md))
        assert result["expose_as_tool"] is False
        assert result["type"] == "automation"

    def test_v2_returns_same_shape_as_v1(self, tmp_path):
        """Both formats must return the same dict keys."""
        v1 = tmp_path / "v1.md"
        v1.write_text("""---
name: test
expose: true
triggers: [tool_call]
---

## Tool: my_tool
Description: A tool
""")
        v2 = tmp_path / "v2.md"
        v2.write_text("""---
name: test
expose_as_tool: true
---

## Tools

### my_tool
A tool
""")
        r1 = _parse_skill_md(str(v1))
        r2 = _parse_skill_md(str(v2))
        # Same top-level keys
        assert set(r1.keys()) == set(r2.keys())


class TestSkillV2Attributes:
    """Test v2 Skill class attributes and methods."""

    def test_has_sense_populated(self):
        """Skills with sense: block should have has_observer=True."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()

        oura = skill_registry.get_skill("oura")
        assert oura is not None
        assert oura.has_observer is True

        # A skill without observer: should be False
        todo = skill_registry.get_skill("todo")
        assert todo is not None
        assert todo.has_observer is False

    def test_has_trigger_simple(self):
        """Test has_trigger with simple string triggers."""
        class DummySkill(Skill):
            async def execute(self, context):
                return SkillResult()

        s = DummySkill()
        # Default triggers from skill_md
        assert s.has_trigger("tool_call")

    def test_tool_names_and_handles(self):
        """Test tool_names() and handles()."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()

        skill = skill_registry.get_skill("memory")
        assert skill is not None
        names = skill.tool_names()
        assert "save_memory" in names
        assert "recall_memory" in names
        assert skill.handles("save_memory")
        assert not skill.handles("nonexistent_tool")

    def test_skill_info_all(self):
        """Test get_skill_info_all returns v2 metadata."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()

        infos = skill_registry.get_skill_info_all()
        assert len(infos) >= 3  # at least memory, reminder, todo
        for info in infos:
            assert "type" in info
            assert "multi_turn" in info
            assert "has_usage_rules" in info
            # V2 fields
            assert "has_observer" in info
            assert "diary_tags" in info
            assert "config_missing" in info


class TestSkillConfigValidation:
    """Test requires_config auto-disable for skills."""

    def test_config_missing_set_on_discovery(self, monkeypatch):
        """Skills with missing required config should have _config_missing set."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()

        # Ensure oura config vars are NOT set
        monkeypatch.delenv("OURA_CLIENT_ID", raising=False)
        monkeypatch.delenv("OURA_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OURA_REFRESH_TOKEN", raising=False)

        skill_registry.discover()
        oura = skill_registry.get_skill("oura")
        assert oura is not None
        assert len(oura._config_missing) > 0
        assert "OURA_CLIENT_ID" in oura._config_missing

    def test_config_missing_excludes_from_get_tools(self, monkeypatch):
        """get_tools() should exclude skills with missing config."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()

        monkeypatch.delenv("OURA_CLIENT_ID", raising=False)
        monkeypatch.delenv("OURA_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OURA_REFRESH_TOKEN", raising=False)

        skill_registry.discover()
        tools = skill_registry.get_tools()
        tool_names = [t["function"]["name"] for t in tools]
        assert "get_oura_data" not in tool_names

    def test_config_present_no_missing(self, monkeypatch):
        """Skills with all config present should have empty _config_missing."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()

        # memory skill has no requires_config
        skill_registry.discover()
        memory = skill_registry.get_skill("memory")
        assert memory is not None
        assert memory._config_missing == []


class TestSkillDiscovery:
    def test_discover_built_in_skills(self, tmp_path, monkeypatch):
        """Test that built-in skills (memory, reminder, todo) are discoverable."""
        import mochi.skills as skill_registry
        # Clear existing state
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()

        names = skill_registry.discover()
        assert "memory" in names
        assert "reminder" in names
        assert "todo" in names

    def test_get_tools_returns_list(self):
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()
        tools = skill_registry.get_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0
        # All tools should have function.name
        for t in tools:
            assert "function" in t
            assert "name" in t["function"]


class TestSkillExecution:
    @pytest.mark.asyncio
    async def test_todo_skill_add(self, tmp_path, monkeypatch):
        """Test todo skill execution."""
        import mochi.db as db_module
        import mochi.skills as skill_registry
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()
        skill_registry.init_all_skill_schemas()

        from mochi.skills.todo.handler import TodoSkill
        skill = TodoSkill()
        ctx = SkillContext(
            trigger="tool_call",
            user_id=1,
            args={"action": "add", "task": "Write tests"},
        )
        result = await skill.execute(ctx)
        assert result.success
        assert "Write tests" in result.output

    @pytest.mark.asyncio
    async def test_reminder_skill_create(self, tmp_path, monkeypatch):
        """Test reminder skill execution."""
        import mochi.db as db_module
        import mochi.skills as skill_registry
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()
        skill_registry.init_all_skill_schemas()

        from mochi.skills.reminder.handler import ReminderSkill
        skill = ReminderSkill()
        ctx = SkillContext(
            trigger="tool_call",
            user_id=1,
            channel_id=100,
            args={"action": "create", "message": "Stand up", "remind_at": "2030-01-01T12:00:00"},
        )
        result = await skill.execute(ctx)
        assert result.success
        assert "Stand up" in result.output


class TestConfigSchema:
    """Tests for ## Config section parsing in SKILL.md."""

    def test_parse_config_schema(self, tmp_path):
        """Parse ## Config table from SKILL.md."""
        md = tmp_path / "SKILL.md"
        md.write_text("""---
name: test_skill
requires_config: [MY_KEY]
---

## Config
| Key | Type | Secret | Default | Description |
|-----|------|--------|---------|-------------|
| MY_KEY | string | yes | | API key for service |
| MY_LAT | string | no | 40.7 | Latitude |
""")
        result = _parse_skill_md(str(md))
        schema = result["config_schema"]
        assert len(schema) == 2

        key_entry = schema[0]
        assert key_entry["key"] == "MY_KEY"
        assert key_entry["type"] == "string"
        assert key_entry["secret"] is True
        assert key_entry["default"] == ""

        lat_entry = schema[1]
        assert lat_entry["key"] == "MY_LAT"
        assert lat_entry["secret"] is False
        assert lat_entry["default"] == "40.7"

    def test_parse_config_schema_empty(self, tmp_path):
        """Skills without ## Config should have empty schema."""
        md = tmp_path / "SKILL.md"
        md.write_text("---\nname: no_config\n---\n")
        result = _parse_skill_md(str(md))
        assert result["config_schema"] == []

    def test_real_skill_config_schema(self):
        """Oura SKILL.md should have config schema (front-matter config: block)."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()

        oura = skill_registry.get_skill("oura")
        assert oura is not None
        # Oura config now has diary_journal and diary_today_ctx (from front-matter config:)
        # Credentials (OURA_CLIENT_ID etc.) moved to requires: env:
        assert len(oura.config_schema) == 2
        keys = [e["key"] for e in oura.config_schema]
        assert "diary_journal" in keys
        assert "diary_today_ctx" in keys

    def test_weather_config_schema(self):
        """Weather SKILL.md should have config schema (front-matter config: block)."""
        import mochi.skills as skill_registry
        skill_registry._skills.clear()
        skill_registry._tool_map.clear()
        skill_registry.discover()

        weather = skill_registry.get_skill("weather")
        assert weather is not None
        assert len(weather.config_schema) == 1
        keys = [e["key"] for e in weather.config_schema]
        assert "WEATHER_CITY" in keys


class TestSkillConfigDb:
    """Tests for per-skill config DB storage."""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        import mochi.skills as skill_registry
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()
        skill_registry.init_all_skill_schemas()

    def test_get_set_skill_config(self):
        from mochi.db import get_skill_config, set_skill_config
        # Initially empty
        assert get_skill_config("oura") == {}
        # Set a value
        set_skill_config("oura", "OURA_CLIENT_ID", "my-client-id")
        config = get_skill_config("oura")
        assert config["OURA_CLIENT_ID"] == "my-client-id"

    def test_set_skill_config_upsert(self):
        from mochi.db import get_skill_config, set_skill_config
        set_skill_config("oura", "OURA_CLIENT_ID", "old-value")
        set_skill_config("oura", "OURA_CLIENT_ID", "new-value")
        assert get_skill_config("oura")["OURA_CLIENT_ID"] == "new-value"

    def test_delete_skill_config(self):
        from mochi.db import get_skill_config, set_skill_config, delete_skill_config
        set_skill_config("oura", "OURA_CLIENT_ID", "val")
        delete_skill_config("oura", "OURA_CLIENT_ID")
        assert get_skill_config("oura") == {}

    def test_skill_config_excludes_internal_keys(self):
        """get_skill_config() should not return _enabled key."""
        from mochi.db import get_skill_config, set_skill_enabled, set_skill_config
        set_skill_enabled("oura", False)
        set_skill_config("oura", "OURA_CLIENT_ID", "val")
        config = get_skill_config("oura")
        assert "_enabled" not in config
        assert "OURA_CLIENT_ID" in config

    def test_skill_config_isolation(self):
        """Config for skill A should not leak into skill B."""
        from mochi.db import get_skill_config, set_skill_config
        set_skill_config("oura", "OURA_CLIENT_ID", "oura-key")
        set_skill_config("weather", "WEATHER_CITY", "Tokyo")
        assert "OURA_CLIENT_ID" in get_skill_config("oura")
        assert "OURA_CLIENT_ID" not in get_skill_config("weather")


class TestSkillGetConfig:
    """Tests for Skill.get_config() priority chain."""

    @pytest.fixture(autouse=True)
    def fresh_db(self, tmp_path, monkeypatch):
        import mochi.db as db_module
        import mochi.skills as skill_registry
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()
        skill_registry.init_all_skill_schemas()

    def test_get_config_from_env(self, monkeypatch):
        """get_config() should fall back to env when DB is empty."""
        monkeypatch.setenv("TEST_KEY", "env-value")

        class DummySkill(Skill):
            async def execute(self, ctx):
                return SkillResult()

        s = DummySkill()
        s._name = "dummy"
        assert s.get_config("TEST_KEY") == "env-value"

    def test_get_config_db_overrides_env(self, monkeypatch):
        """DB value should take priority over env."""
        monkeypatch.setenv("TEST_KEY", "env-value")
        from mochi.db import set_skill_config
        set_skill_config("dummy", "TEST_KEY", "db-value")

        class DummySkill(Skill):
            async def execute(self, ctx):
                return SkillResult()

        s = DummySkill()
        s._name = "dummy"
        assert s.get_config("TEST_KEY") == "db-value"

    def test_get_config_schema_default(self):
        """get_config() should use schema default when DB and env are empty."""
        class DummySkill(Skill):
            async def execute(self, ctx):
                return SkillResult()

        s = DummySkill()
        s._name = "dummy"
        s.config_schema = [{"key": "MY_PORT", "type": "string", "secret": False,
                            "default": "8080", "description": "Port"}]
        assert s.get_config("MY_PORT") == "8080"

    def test_get_config_empty_fallback(self):
        """get_config() should return empty string when nothing matches."""
        class DummySkill(Skill):
            async def execute(self, ctx):
                return SkillResult()

        s = DummySkill()
        s._name = "dummy"
        assert s.get_config("NONEXISTENT") == ""
