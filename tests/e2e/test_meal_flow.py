"""E2E tests for the meal skill: log → query → delete via mock LLM."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from mochi.transport import IncomingMessage
from mochi.ai_client import chat
from mochi.db import query_health_log
from tests.e2e.mock_llm import make_response, make_tool_call


def _msg(text: str, user_id: int = 1, channel_id: int = 100) -> IncomingMessage:
    return IncomingMessage(
        user_id=user_id, channel_id=channel_id,
        text=text, transport="fake",
    )


class TestMealLog:
    """LLM calls log_meal tool to record a meal."""

    @pytest.mark.asyncio
    async def test_log_meal_basic(self, mock_llm_factory):
        """Log a breakfast, verify it lands in health_log."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "breakfast",
                    "items": json.dumps([
                        {"name": "鸡蛋", "calories": 70, "protein_g": 6, "carbs_g": 1, "fat_g": 5},
                        {"name": "面包", "calories": 150, "protein_g": 4, "carbs_g": 28, "fat_g": 2},
                    ]),
                    "total_calories": 220,
                    "total_protein_g": 10,
                    "total_carbs_g": 29,
                    "total_fat_g": 7,
                }),
            ]),
            make_response("记下了～早餐220kcal"),
        ])

        reply = await chat(_msg("早上吃了鸡蛋和面包"))

        assert "220" in reply.text or "记" in reply.text
        records = query_health_log(user_id=1, types=["meal"], days=1)
        assert len(records) == 1
        metrics = json.loads(records[0]["metrics"])
        assert metrics["meal_type"] == "breakfast"
        assert metrics["total"]["calories"] == 220
        assert len(metrics["items"]) == 2

    @pytest.mark.asyncio
    async def test_log_meal_with_date(self, mock_llm_factory):
        """Log a meal with explicit date."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "lunch",
                    "items": json.dumps([{"name": "拉面", "calories": 500}]),
                    "total_calories": 500,
                    "date": "2026-04-10",
                }),
            ]),
            make_response("已记录"),
        ])

        await chat(_msg("昨天午饭吃了拉面"))

        records = query_health_log(user_id=1, types=["meal"], date="2026-04-10")
        assert len(records) == 1
        assert "拉面" in records[0]["content"]

    @pytest.mark.asyncio
    async def test_log_meal_invalid_type(self, mock_llm_factory):
        """Invalid meal_type returns error."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "brunch",
                    "items": json.dumps([{"name": "eggs", "calories": 100}]),
                    "total_calories": 100,
                }),
            ]),
            make_response("Sorry, that didn't work."),
        ])

        await chat(_msg("I had brunch"))

        # No meal should be saved
        records = query_health_log(user_id=1, types=["meal"], days=1)
        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_log_meal_snack_allows_multiple(self, mock_llm_factory):
        """Multiple snacks on the same day should all be saved (unique source)."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "snack",
                    "items": json.dumps([{"name": "苹果", "calories": 80}]),
                    "total_calories": 80,
                    "date": "2026-04-11",
                }),
            ]),
            make_response("记下了"),
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "snack",
                    "items": json.dumps([{"name": "饼干", "calories": 150}]),
                    "total_calories": 150,
                    "date": "2026-04-11",
                }),
            ]),
            make_response("也记下了"),
        ])

        # First snack at 10:00:00
        t1 = datetime(2026, 4, 11, 10, 0, 0, tzinfo=timezone.utc)
        with patch("mochi.skills.meal.handler.datetime") as mock_dt:
            mock_dt.now.return_value = t1
            mock_dt.strptime = datetime.strptime
            await chat(_msg("吃了一个苹果"))

        # Second snack at 15:30:00 (different timestamp → unique source key)
        t2 = datetime(2026, 4, 11, 15, 30, 0, tzinfo=timezone.utc)
        with patch("mochi.skills.meal.handler.datetime") as mock_dt:
            mock_dt.now.return_value = t2
            mock_dt.strptime = datetime.strptime
            await chat(_msg("又吃了饼干"))

        records = query_health_log(user_id=1, types=["meal"], date="2026-04-11")
        # Both snacks should exist (different source timestamps)
        assert len(records) >= 2


class TestMealQuery:
    """LLM calls query_meals tool to retrieve history."""

    @pytest.mark.asyncio
    async def test_query_meals_empty(self, mock_llm_factory):
        """Query with no records returns empty message."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("query_meals", {"days": 1}),
            ]),
            make_response("今天还没有饮食记录"),
        ])

        reply = await chat(_msg("今天吃了什么"))

        assert "没" in reply.text or "无" in reply.text or "还没" in reply.text

    @pytest.mark.asyncio
    async def test_query_after_log(self, mock_llm_factory):
        """Log a meal then query — should see it in results."""
        mock_llm_factory([
            # First: log
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "dinner",
                    "items": json.dumps([{"name": "牛排", "calories": 400, "protein_g": 35}]),
                    "total_calories": 400,
                    "total_protein_g": 35,
                }),
            ]),
            make_response("记下了"),
            # Second: query
            make_response(tool_calls=[
                make_tool_call("query_meals", {"days": 1}),
            ]),
            make_response("今天晚餐吃了牛排 400kcal"),
        ])

        await chat(_msg("晚饭吃了牛排"))
        reply = await chat(_msg("今天吃了什么"))

        assert "牛排" in reply.text or "400" in reply.text


class TestMealDelete:
    """LLM calls delete_meal tool to remove a record."""

    @pytest.mark.asyncio
    async def test_delete_meal(self, mock_llm_factory):
        """Log then delete a meal — record should be removed."""
        mock_llm_factory([
            # Log
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "lunch",
                    "items": json.dumps([{"name": "沙拉", "calories": 200}]),
                    "total_calories": 200,
                }),
            ]),
            make_response("记下了"),
            # Delete
            make_response(tool_calls=[
                make_tool_call("delete_meal", {"meal_type": "lunch"}),
            ]),
            make_response("已删除"),
        ])

        await chat(_msg("午饭吃了沙拉"))

        # Verify meal exists
        records = query_health_log(user_id=1, types=["meal"], days=1)
        assert len(records) == 1

        await chat(_msg("刚才午饭记错了，删掉"))

        # Verify meal gone
        records = query_health_log(user_id=1, types=["meal"], days=1)
        assert len(records) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_meal(self, mock_llm_factory):
        """Deleting a meal that doesn't exist should report not found."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("delete_meal", {"meal_type": "breakfast"}),
            ]),
            make_response("没有找到早餐记录"),
        ])

        reply = await chat(_msg("删掉今天早餐"))

        assert "没" in reply.text or "找不到" in reply.text


class TestMealUpsert:
    """Same meal_type on same day should upsert (replace), not duplicate."""

    @pytest.mark.asyncio
    async def test_same_meal_upserts(self, mock_llm_factory):
        """Logging breakfast twice on the same day should result in one record."""
        mock_llm_factory([
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "breakfast",
                    "items": json.dumps([{"name": "粥", "calories": 100}]),
                    "total_calories": 100,
                    "date": "2026-04-11",
                }),
            ]),
            make_response("记下了"),
            make_response(tool_calls=[
                make_tool_call("log_meal", {
                    "meal_type": "breakfast",
                    "items": json.dumps([{"name": "麦片", "calories": 300}]),
                    "total_calories": 300,
                    "date": "2026-04-11",
                }),
            ]),
            make_response("更新了"),
        ])

        await chat(_msg("早上喝了粥"))
        await chat(_msg("不对，早上吃的是麦片"))

        records = query_health_log(user_id=1, types=["meal"], date="2026-04-11")
        # breakfast should be upserted — only 1 record
        breakfast_records = [
            r for r in records
            if json.loads(r.get("metrics") or "{}").get("meal_type") == "breakfast"
        ]
        assert len(breakfast_records) == 1
        metrics = json.loads(breakfast_records[0]["metrics"])
        assert metrics["total"]["calories"] == 300
        assert metrics["items"][0]["name"] == "麦片"
