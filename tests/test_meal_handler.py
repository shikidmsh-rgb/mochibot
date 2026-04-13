"""Tests for mochi/skills/meal/handler.py — MealSkill and _normalize_meal_items."""

import json
import pytest
from unittest.mock import patch, MagicMock

from mochi.skills.base import SkillContext, SkillResult
from mochi.skills.meal.handler import MealSkill


def _make_ctx(tool_name: str, user_id: int = 1, **kwargs) -> SkillContext:
    return SkillContext(
        trigger="tool_call", user_id=user_id, tool_name=tool_name, args=kwargs,
    )


class TestNormalizeMealItems:

    def test_normalize_list(self):
        skill = MealSkill()
        raw = [{"name": "Rice", "calories": 200, "protein_g": 4}]
        result = skill._normalize_meal_items(raw)
        assert len(result) == 1
        assert result[0]["name"] == "Rice"
        assert result[0]["calories"] == 200
        assert result[0]["fat_g"] == 0.0

    def test_normalize_json_string(self):
        skill = MealSkill()
        raw = json.dumps([{"name": "Egg", "calories": 70}])
        result = skill._normalize_meal_items(raw)
        assert len(result) == 1
        assert result[0]["name"] == "Egg"

    def test_normalize_invalid_json_string(self):
        skill = MealSkill()
        result = skill._normalize_meal_items("not json at all")
        assert result == []

    def test_normalize_non_list(self):
        skill = MealSkill()
        result = skill._normalize_meal_items({"name": "Egg"})
        assert result == []

    def test_normalize_skips_items_without_name(self):
        skill = MealSkill()
        raw = [{"calories": 100}, {"name": "Bread", "calories": 150}]
        result = skill._normalize_meal_items(raw)
        assert len(result) == 1
        assert result[0]["name"] == "Bread"

    def test_normalize_skips_non_dict_items(self):
        skill = MealSkill()
        raw = ["not a dict", {"name": "Apple", "calories": 50}]
        result = skill._normalize_meal_items(raw)
        assert len(result) == 1


class TestMealSkillLog:

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.save_health_log", return_value=1)
    async def test_log_success(self, mock_save):
        skill = MealSkill()
        items = json.dumps([{"name": "Chicken", "calories": 300, "protein_g": 30}])
        ctx = _make_ctx(
            "log_meal", meal_type="lunch", items=items,
            total_calories=300, total_protein_g=30, date="2026-01-15",
        )
        result = await skill.execute(ctx)
        assert result.success is True
        mock_save.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_invalid_meal_type(self):
        skill = MealSkill()
        items = json.dumps([{"name": "Food", "calories": 100}])
        ctx = _make_ctx("log_meal", meal_type="brunch", items=items)
        result = await skill.execute(ctx)
        assert result.success is False
        assert "meal_type must be one of" in result.output

    @pytest.mark.asyncio
    async def test_log_empty_items(self):
        skill = MealSkill()
        ctx = _make_ctx("log_meal", meal_type="breakfast", items="[]")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "non-empty" in result.output

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.save_health_log", return_value=2)
    async def test_log_invalid_date_format(self, mock_save):
        skill = MealSkill()
        items = json.dumps([{"name": "Toast", "calories": 100}])
        ctx = _make_ctx("log_meal", meal_type="breakfast", items=items, date="Jan 1")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "invalid date" in result.output.lower()


class TestMealSkillQuery:

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.query_health_log", return_value=[])
    async def test_query_no_records(self, mock_query):
        skill = MealSkill()
        ctx = _make_ctx("query_meals", date="2026-01-01")
        result = await skill.execute(ctx)
        assert "无饮食记录" in result.output

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.query_health_log")
    async def test_query_with_data(self, mock_query):
        mock_query.return_value = [
            {
                "id": 1,
                "date": "2026-01-15",
                "metrics": json.dumps({
                    "meal_type": "lunch",
                    "items": [{"name": "Rice"}],
                    "total": {"calories": 400, "protein_g": 10, "carbs_g": 50, "fat_g": 8},
                }),
            },
        ]
        skill = MealSkill()
        ctx = _make_ctx("query_meals", date="2026-01-15")
        result = await skill.execute(ctx)
        assert "2026-01-15" in result.output
        assert "Rice" in result.output

    @pytest.mark.asyncio
    async def test_query_invalid_date(self):
        skill = MealSkill()
        ctx = _make_ctx("query_meals", date="bad-date")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "invalid date" in result.output.lower()


class TestMealSkillDelete:

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.delete_health_log_items", return_value=1)
    @patch("mochi.skills.meal.handler.query_health_log")
    async def test_delete_existing_meal(self, mock_query, mock_delete):
        mock_query.return_value = [
            {
                "id": 10,
                "date": "2026-01-15",
                "metrics": json.dumps({"meal_type": "breakfast"}),
            },
        ]
        skill = MealSkill()
        ctx = _make_ctx("delete_meal", meal_type="breakfast", date="2026-01-15")
        result = await skill.execute(ctx)
        assert result.success is True
        mock_delete.assert_called_once_with([10])

    @pytest.mark.asyncio
    @patch("mochi.skills.meal.handler.query_health_log", return_value=[])
    async def test_delete_not_found(self, mock_query):
        skill = MealSkill()
        ctx = _make_ctx("delete_meal", meal_type="dinner", date="2026-01-15")
        result = await skill.execute(ctx)
        assert "没有找到" in result.output

    @pytest.mark.asyncio
    async def test_delete_invalid_meal_type(self):
        skill = MealSkill()
        ctx = _make_ctx("delete_meal", meal_type="brunch", date="2026-01-15")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "meal_type must be one of" in result.output


class TestMealSkillUnknownTool:

    @pytest.mark.asyncio
    async def test_unknown_tool_name(self):
        skill = MealSkill()
        ctx = _make_ctx("unknown_meal_tool")
        result = await skill.execute(ctx)
        assert result.success is False
        assert "Unknown meal tool" in result.output
