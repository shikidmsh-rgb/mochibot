"""Tests for skill registry and SKILL.md parsing."""

import pytest
from mochi.skills.base import _parse_skill_md, Skill, SkillContext, SkillResult


class TestSkillMdParser:
    def test_parse_nonexistent(self):
        result = _parse_skill_md("/nonexistent/SKILL.md")
        assert result["expose_as_tool"] is True
        assert result["triggers"] == ["tool_call"]
        assert result["tools"] == []

    def test_parse_real_skill(self, tmp_path):
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
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()

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
        monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
        db_module.init_db()

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
