"""Released daily-tool contracts and their durable state receipts."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from mochi import skills
from mochi.db import _connect
from mochi.skills.base import SkillContext
from mochi.skills.habit.handler import _current_period
from mochi.skills.habit.queries import (
    add_habit, get_habit_checkins, list_habits, record_habit_progress,
)
from mochi.skills.meal.queries import query_health_log, save_health_log
from mochi.skills.reminder import queries as reminders
from mochi.skills.todo.queries import create_todo, get_todos
from mochi.tool_availability import ToolAvailability


async def _call(skill, tool, *, user_id=1, **args):
    return await skills.get_skill(skill).execute(SkillContext(
        trigger="tool_call", user_id=user_id, channel_id=100,
        transport="fake", actor="main", owner_authorized=True,
        tool_name=tool, args=args,
    ))


@pytest.mark.asyncio
async def test_habit_progress_reconciles_counts_and_restores_original_identity():
    created = await _call(
        "habit", "edit_habit", action="add", name="Water", cycle="daily", target=2,
    )
    assert created.success and created.state_changed
    habit_id = list_habits(1)[0]["id"]
    assert created.entity_refs == [f"habit:{habit_id}"]
    added = await _call("habit", "habit_progress", action="add", habit_name="Water")
    synced = await _call("habit", "habit_progress", action="sync", habit_name="Water", total=4)
    repeated = await _call("habit", "habit_progress", action="sync", habit_id=habit_id, total=4)
    backwards = await _call("habit", "habit_progress", action="sync", habit_id=habit_id, total=3)
    assert added.state_changed and synced.state_changed
    assert "4/2" in synced.output and "+3" in synced.output
    assert repeated.success and not repeated.state_changed
    assert not backwards.success and not backwards.state_changed
    undone = await _call("habit", "habit_progress", action="undo", habit_name="Water")
    assert undone.state_changed and "3/2" in undone.output
    for invalid in (
        {"count": True}, {"count": 0}, {"count": 1.5}, {"total": 5},
    ):
        rejected = await _call("habit", "habit_progress", action="add", habit_id=habit_id, **invalid)
        assert not rejected.success and not rejected.state_changed
    assert len(get_habit_checkins(habit_id, _current_period("daily"))) == 3
    mismatch = await _call(
        "habit", "habit_progress", action="add", habit_id=habit_id, habit_name="Wrong",
    )
    other_owner = await _call("habit", "habit_progress", action="undo", user_id=2, habit_id=habit_id)
    assert not mismatch.success and not other_owner.success
    for _ in range(2):
        result = await _call("habit", "edit_habit", action="remove", habit_id=habit_id)
        assert result.success
    assert not result.state_changed
    restored = await _call(
        "habit", "edit_habit", action="add", name="Water", cycle="daily", target=5,
    )
    assert restored.success and "reactivated" in restored.output
    assert list_habits(1)[0]["id"] == habit_id
    assert len(get_habit_checkins(habit_id, _current_period("daily"))) == 3


@pytest.mark.asyncio
async def test_habit_simple_schedule_snapshot_and_atomic_sync():
    created = await _call(
        "habit", "edit_habit", action="add", name="Walk", cycle="weekly",
        target=3, weekdays=["sun", "sat", "sat"],
    )
    assert created.success
    habit_id = list_habits(1)[0]["id"]
    assert list_habits(1)[0]["frequency"] == "weekly_on:sat,sun:3"
    period = _current_period("weekly")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: record_habit_progress(habit_id, 1, period, total=2), range(2),
        ))
    assert sorted(results) == [(2, 0), (2, 2)]
    for action, args in (
        ("update", {"target": 4, "category": "health"}),
        ("pause", {"until": "2099-01-01"}),
        ("resume", {}),
    ):
        changed = await _call("habit", "edit_habit", action=action, habit_name="Walk", **args)
        repeated = await _call("habit", "edit_habit", action=action, habit_name="Walk", **args)
        assert changed.success and changed.state_changed
        assert repeated.success and not repeated.state_changed
    snapshot = skills.get_skill("habit").progress_context(1)
    assert "2/4" in snapshot and "周六、周日" in snapshot
    assert "weekly_on" not in snapshot
    assert skills.get_skill("habit").progress_context(2) == "No active habits."
    changed = await _call("habit", "edit_habit", action="update", habit_id=habit_id, cycle="daily")
    assert changed.success and list_habits(1)[0]["frequency"] == "daily:4"
    invalid = await _call(
        "habit", "edit_habit", action="update", habit_id=habit_id, weekdays=["mon"],
    )
    assert not invalid.success
    for index in range(9):
        add_habit(1, f"Extra {index}", "daily:1")
    snapshot = skills.get_skill("habit").progress_context(1)
    assert snapshot.count("\n") == 8 and "2 more active habits" in snapshot


@pytest.mark.asyncio
async def test_todo_exact_match_reopen_and_clear_date_preserve_noop_facts():
    tid = create_todo(1, "Ｂuy  MILK", nudge_date="2099-01-01")
    create_todo(2, "Buy milk")
    completed = await _call("todo", "manage_todo", action="complete", match="buy milk")
    repeated = await _call("todo", "manage_todo", action="complete", todo_id=tid)
    assert completed.state_changed and repeated.success and not repeated.state_changed
    reopened = await _call("todo", "manage_todo", action="reopen", match="buy milk")
    assert reopened.state_changed and not get_todos(1)[0]["done"]
    conn = _connect()
    assert conn.execute("SELECT completed_at FROM todos WHERE id = ?", (tid,)).fetchone()[0] is None
    conn.close()
    assert not (await _call("todo", "manage_todo", action="reopen", todo_id=tid)).state_changed
    duplicate = create_todo(1, "Buy milk")
    before = get_todos(1, include_done=True)
    for match in ("buy milk", "milk"):
        rejected = await _call("todo", "manage_todo", action="complete", match=match)
        assert not rejected.success and not rejected.state_changed
    assert get_todos(1, include_done=True) == before
    cleared = await _call(
        "todo", "manage_todo", action="update", todo_id=tid, clear_nudge_date=True,
    )
    assert cleared.state_changed and get_todos(1)[0]["nudge_date"] is None
    assert not (await _call(
        "todo", "manage_todo", action="update", todo_id=tid, clear_nudge_date=True,
    )).state_changed
    conflict = await _call(
        "todo", "manage_todo", action="update", todo_id=tid,
        clear_nudge_date=True, nudge_date="2099-01-01",
    )
    wrong_owner = await _call("todo", "manage_todo", user_id=2, action="delete", todo_id=duplicate)
    assert not conflict.success and not wrong_owner.success


@pytest.mark.asyncio
async def test_meal_array_totals_slots_and_exact_owner_scoped_deletion(monkeypatch):
    from mochi.skills.meal import handler, queries
    monkeypatch.setattr(handler, "logical_today", lambda: "2026-09-23")
    monkeypatch.setattr(queries, "logical_days_ago", lambda days: "2026-09-23" if days == 0 else "2026-09-22")
    items = [
        {"name": "Rice", "calories": 200, "protein_g": 4.2, "carbs_g": 42, "fat_g": 0.5},
        {"name": "Tofu", "calories": 80, "protein_g": 8.3, "carbs_g": 2, "fat_g": 4},
    ]
    for bad in (json.dumps(items), [items[0], {"name": "missing"}], [{**items[0], "calories": True}],
                [{**items[0], "fat_g": float("inf")}], [{**items[0], "carbs_g": -1}]):
        result = await _call("meal", "log_meal", meal_type="lunch", items=bad)
        assert not result.success and query_health_log(1, date="2026-09-23") == []
    first = await _call("meal", "log_meal", meal_type="lunch", items=items)
    repeated = await _call("meal", "log_meal", meal_type="lunch", items=items[:1])
    assert first.entity_refs == repeated.entity_refs
    for _ in range(2):
        assert (await _call("meal", "log_meal", meal_type="snack", items=items)).state_changed
    records = query_health_log(1, date="2026-09-23")
    assert len(records) == 3
    metrics = json.loads(records[-1]["metrics"])
    assert metrics["total"] == {"calories": 280, "protein_g": 12.5, "carbs_g": 44, "fat_g": 4.5}
    save_health_log(1, "2026-09-22", "meal", "yesterday", source="old")
    listed = await _call("meal", "query_meals", days=1)
    assert "2026-09-22" not in listed.output
    assert all(f"#{row['id']}" in listed.output for row in records)
    meal_id = records[-1]["id"]
    assert not (await _call("meal", "delete_meal", user_id=2, meal_id=meal_id)).success
    deleted = await _call("meal", "delete_meal", meal_id=meal_id)
    assert deleted.state_changed and len(query_health_log(1, date="2026-09-23")) == 2
    assert not (await _call("meal", "delete_meal", meal_id=meal_id)).success


def test_daily_schema_keeps_name_and_requires_typed_food_objects():
    availability = ToolAvailability.from_definitions(skills.get_tools(), source="test")
    assert {"habit_progress", "schedule_self_reminder"} <= availability.names
    assert not {"checkin_habit", "query_habit"} & availability.names
    assert "name" in availability.parameters_for("edit_habit")["properties"]
    assert {t["function"]["name"] for t in skills.get_tools_by_load("resident")} >= {"schedule_self_reminder"}
    valid = {"meal_type": "lunch", "items": [{
        "name": "Rice", "calories": 100, "protein_g": 2, "carbs_g": 22, "fat_g": 0.2,
    }]}
    assert availability.validate_arguments("log_meal", valid) is None
    for invalid in (
        {**valid, "total_calories": 100},
        {**valid, "items": []},
        {**valid, "items": json.dumps(valid["items"])},
        {**valid, "items": [{"name": "missing"}]},
        {**valid, "items": [{**valid["items"][0], "extra": 1}]},
        {**valid, "items": [{**valid["items"][0], "calories": True}]},
        {**valid, "items": [{**valid["items"][0], "fat_g": float("inf")}]},
    ):
        assert availability.validate_arguments("log_meal", invalid) is not None


@pytest.mark.asyncio
async def test_recurring_reminder_update_preserves_started_occurrence(monkeypatch):
    now = datetime.now(timezone.utc)
    due = now + timedelta(hours=1)
    monkeypatch.setattr(reminders, "_now", lambda: now)
    created = await _call(
        "reminder", "schedule_self_reminder", intent="Review progress",
        remind_at=due.isoformat(), recurrence="weekdays",
    )
    assert created.success and created.state_changed
    rid = reminders.get_active_reminders(1)[0]["id"]
    assert "repeats weekdays" in created.output and "Expires at" in created.output
    repeated = await _call("reminder", "manage_reminder", action="update", reminder_id=rid, recurrence="weekdays")
    assert repeated.success and not repeated.state_changed
    changed = await _call("reminder", "manage_reminder", action="update", reminder_id=rid, recurrence="one_time")
    assert changed.state_changed and reminders.get_active_reminders(1)[0]["recurrence"] is None
    assert not (await _call("reminder", "manage_reminder", user_id=2, action="update", reminder_id=rid, intent="Wrong")).success
    claim = reminders.claim_reminder(rid, now=due)
    assert claim is not None
    assert reminders.claim_reminder(rid, now=due) is None
    for action, args in (("update", {"intent": "Changed"}), ("delete", {})):
        rejected = await _call("reminder", "manage_reminder", action=action, reminder_id=rid, **args)
        assert not rejected.success and not rejected.state_changed
    assert reminders.record_reminder_failure(rid, claim["claimed_at"], "failed", now=due) is not None
    rejected = await _call("reminder", "manage_reminder", action="update", reminder_id=rid, intent="Changed")
    assert not rejected.success
    assert reminders.get_active_reminders(1)[0]["context"] == "Review progress"
    friday = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)
    assert reminders.compute_next_occurrence(friday, "weekdays") == friday + timedelta(days=3)
