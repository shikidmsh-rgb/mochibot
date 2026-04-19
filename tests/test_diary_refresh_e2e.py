"""End-to-end integration: skill writes propagate to diary 今日状態.

Exercises the full chain:
  Skill.run() → execute() writes DB → framework refreshes diary → file content updates

Why this layer matters: each lower test layer (framework hook, refresh function,
handler unit, diary_status return value) is green individually, but nothing
asserts the contract *between* layers. This file plugs that gap by reading
diary.md after each write and asserting visibility — the exact failure mode
that originally let the "todo not unchecked" bug ship.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from mochi.diary import DailyFile
import mochi.diary as diary_mod
from mochi.skills import get_skill
from mochi.skills.base import SkillContext


@pytest.fixture
def diary_file(tmp_path, monkeypatch):
    """Replace the global diary instance with one backed by tmp_path."""
    test_diary = DailyFile(
        path=tmp_path / "diary.md",
        label="Diary",
        max_lines=200,
        sections=("今日状態", "今日日記"),
        section_max_lines={"今日状態": 100, "今日日記": 50},
    )
    monkeypatch.setattr(diary_mod, "diary", test_diary)
    return test_diary


def status_block(diary_file: DailyFile) -> str:
    return diary_file.read(section="今日状態") or ""


def _ctx(args: dict, *, tool_name: str = "", user_id: int = 1) -> SkillContext:
    return SkillContext(
        trigger="tool_call",
        user_id=user_id,
        tool_name=tool_name,
        args=args,
    )


# ── Todo ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_todo_add_appears_in_diary(diary_file):
    todo = get_skill("todo")
    result = await todo.run(_ctx(
        {"action": "add", "task": "buy oat milk"},
        tool_name="manage_todo",
    ))
    assert result.success
    assert "buy oat milk" in status_block(diary_file)


@pytest.mark.asyncio
async def test_todo_complete_removes_from_diary(diary_file):
    todo = get_skill("todo")
    add = await todo.run(_ctx(
        {"action": "add", "task": "feed cat"}, tool_name="manage_todo"))
    assert "feed cat" in status_block(diary_file)

    todo_id = int(re.search(r"Todo #(\d+)", add.output).group(1))
    await todo.run(_ctx(
        {"action": "complete", "todo_id": todo_id}, tool_name="manage_todo"))
    assert "feed cat" not in status_block(diary_file)


@pytest.mark.asyncio
async def test_todo_delete_removes_from_diary(diary_file):
    todo = get_skill("todo")
    add = await todo.run(_ctx(
        {"action": "add", "task": "renew passport"}, tool_name="manage_todo"))
    assert "renew passport" in status_block(diary_file)

    todo_id = int(re.search(r"Todo #(\d+)", add.output).group(1))
    await todo.run(_ctx(
        {"action": "delete", "todo_id": todo_id}, tool_name="manage_todo"))
    assert "renew passport" not in status_block(diary_file)


@pytest.mark.asyncio
async def test_todo_failed_action_leaves_diary_intact(diary_file):
    """Failed write must not corrupt diary and must not crash."""
    todo = get_skill("todo")
    await todo.run(_ctx(
        {"action": "add", "task": "real task"}, tool_name="manage_todo"))
    block_before = status_block(diary_file)

    bad = await todo.run(_ctx(
        {"action": "complete"}, tool_name="manage_todo"))  # missing todo_id
    assert not bad.success
    block_after = status_block(diary_file)
    assert "real task" in block_after  # original entry preserved


# ── Reminder ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reminder_create_visible_in_diary(diary_file):
    rem = get_skill("reminder")
    # Use end-of-day so the reminder stays in ⏳ state regardless of test wall clock.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    future = f"{today}T23:59:00+00:00"
    result = await rem.run(_ctx({
        "action": "create", "message": "call dentist", "remind_at": future,
    }, tool_name="manage_reminder"))
    assert result.success, result.output
    assert "call dentist" in status_block(diary_file)


@pytest.mark.asyncio
async def test_reminder_delete_removes_from_diary(diary_file):
    rem = get_skill("reminder")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    future = f"{today}T23:59:00+00:00"
    create = await rem.run(_ctx({
        "action": "create", "message": "water plants", "remind_at": future,
    }, tool_name="manage_reminder"))
    assert "water plants" in status_block(diary_file)

    rid = int(re.search(r"Reminder #(\d+)", create.output).group(1))
    await rem.run(_ctx(
        {"action": "delete", "reminder_id": rid}, tool_name="manage_reminder"))
    assert "water plants" not in status_block(diary_file)


# ── Meal ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_meal_log_appears_in_diary(diary_file):
    """Logging breakfast: the food item name should appear in 今日状態."""
    meal = get_skill("meal")
    items = '[{"name": "egg tart", "calories": 230, "protein_g": 4, "carbs_g": 25, "fat_g": 12}]'
    result = await meal.run(_ctx({
        "meal_type": "breakfast",
        "items": items,
        "total_calories": 230,
        "total_protein_g": 4,
        "total_carbs_g": 25,
        "total_fat_g": 12,
    }, tool_name="log_meal"))
    assert result.success, result.output
    assert "egg tart" in status_block(diary_file)


# ── Habit ─────────────────────────────────────────────────────────────────────
# Habit checkin used to manually refresh; now the framework does it.
# This guards against a regression where someone removes the framework hook
# without re-adding the manual call.


@pytest.mark.asyncio
async def test_habit_checkin_progress_updates_diary(diary_file):
    from mochi.skills.habit.queries import add_habit

    hid = add_habit(user_id=1, name="drink water", frequency="daily:3")

    habit = get_skill("habit")
    result = await habit.run(_ctx(
        {"action": "checkin", "habit_id": hid}, tool_name="checkin_habit"))
    assert result.success, result.output

    block = status_block(diary_file)
    assert "drink water" in block
    assert "1/3" in block
