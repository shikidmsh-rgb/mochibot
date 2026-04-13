"""Tests for mochi/skills/todo/handler.py — TodoSkill."""

import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.todo.handler import TodoSkill


def _make_ctx(action: str | None = None, user_id: int = 1, **kwargs) -> SkillContext:
    args = {**kwargs}
    if action is not None:
        args["action"] = action
    return SkillContext(
        trigger="tool_call", user_id=user_id, tool_name="manage_todo", args=args,
    )


class TestTodoAdd:

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.create_todo", return_value=1)
    async def test_add_success(self, mock_create):
        skill = TodoSkill()
        ctx = _make_ctx("add", task="Buy milk")
        result = await skill.execute(ctx)
        assert result.success is True
        assert "#1" in result.output
        assert "Buy milk" in result.output
        mock_create.assert_called_once_with(1, "Buy milk", nudge_date=None)

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.create_todo", return_value=2)
    async def test_add_with_nudge_date(self, mock_create):
        skill = TodoSkill()
        ctx = _make_ctx("add", task="Dentist", nudge_date="2026-05-01")
        result = await skill.execute(ctx)
        assert result.success is True
        assert "2026-05-01" in result.output
        mock_create.assert_called_once_with(1, "Dentist", nudge_date="2026-05-01")

    @pytest.mark.asyncio
    async def test_add_missing_task(self):
        skill = TodoSkill()
        ctx = _make_ctx("add")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "'task' is required" in result.output


class TestTodoList:

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.get_todos", return_value=[])
    async def test_list_empty(self, mock_get):
        skill = TodoSkill()
        ctx = _make_ctx("list")
        result = await skill.execute(ctx)
        assert "No todos found" in result.output

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.get_todos")
    async def test_list_with_items(self, mock_get):
        mock_get.return_value = [
            {"id": 1, "task": "Buy milk", "done": False, "nudge_date": None},
            {"id": 2, "task": "Fix bug", "done": True, "nudge_date": "2026-04-20"},
        ]
        skill = TodoSkill()
        ctx = _make_ctx("list")
        result = await skill.execute(ctx)
        assert "Buy milk" in result.output
        assert "Fix bug" in result.output
        assert "#1" in result.output
        assert "#2" in result.output


class TestTodoComplete:

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.complete_todo", return_value=True)
    async def test_complete_success(self, mock_complete):
        skill = TodoSkill()
        ctx = _make_ctx("complete", todo_id="3")
        result = await skill.execute(ctx)
        assert "completed" in result.output.lower()
        mock_complete.assert_called_once_with(1, 3)

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.complete_todo", return_value=False)
    async def test_complete_not_found(self, mock_complete):
        skill = TodoSkill()
        ctx = _make_ctx("complete", todo_id="999")
        result = await skill.execute(ctx)
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_complete_missing_id(self):
        skill = TodoSkill()
        ctx = _make_ctx("complete")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "'todo_id' is required" in result.output


class TestTodoDelete:

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.delete_todo", return_value=True)
    async def test_delete_success(self, mock_delete):
        skill = TodoSkill()
        ctx = _make_ctx("delete", todo_id="4")
        result = await skill.execute(ctx)
        assert "deleted" in result.output.lower()
        mock_delete.assert_called_once_with(1, 4)

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.delete_todo", return_value=False)
    async def test_delete_not_found(self, mock_delete):
        skill = TodoSkill()
        ctx = _make_ctx("delete", todo_id="888")
        result = await skill.execute(ctx)
        assert "not found" in result.output.lower()


class TestTodoUpdate:

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.update_todo", return_value=True)
    async def test_update_task(self, mock_update):
        skill = TodoSkill()
        ctx = _make_ctx("update", todo_id="5", task="New task text")
        result = await skill.execute(ctx)
        assert "updated" in result.output.lower()
        mock_update.assert_called_once_with(1, 5, task="New task text")

    @pytest.mark.asyncio
    @patch("mochi.skills.todo.handler.update_todo", return_value=True)
    async def test_update_nudge_date(self, mock_update):
        skill = TodoSkill()
        ctx = _make_ctx("update", todo_id="5", nudge_date="2026-06-01")
        result = await skill.execute(ctx)
        assert "updated" in result.output.lower()
        mock_update.assert_called_once_with(1, 5, nudge_date="2026-06-01")

    @pytest.mark.asyncio
    async def test_update_no_fields(self):
        skill = TodoSkill()
        ctx = _make_ctx("update", todo_id="5")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "at least one field" in result.output.lower()

    @pytest.mark.asyncio
    async def test_update_missing_id(self):
        skill = TodoSkill()
        ctx = _make_ctx("update", task="Something")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "'todo_id' is required" in result.output


class TestTodoEdgeCases:

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        skill = TodoSkill()
        ctx = _make_ctx("archive")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "Unknown" in result.output

    @pytest.mark.asyncio
    async def test_no_action_provided(self):
        skill = TodoSkill()
        ctx = _make_ctx(None)
        result = await skill.execute(ctx)
        assert result.success is False
        assert "'action' is required" in result.output
