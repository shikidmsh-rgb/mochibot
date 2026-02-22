"""Tests for the database layer."""

import os
import tempfile
import pytest

# Override DB path BEFORE importing mochi.db
_temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_temp_db.close()

from mochi.db import (
    init_db,
    save_message,
    get_recent_messages,
    get_core_memory,
    update_core_memory,
    create_reminder,
    get_pending_reminders,
    mark_reminder_fired,
    create_todo,
    get_todos,
    complete_todo,
    get_last_user_message_time,
    get_message_count_today,
    get_active_todo_count,
    get_upcoming_reminders,
    save_memory_item,
    recall_memory,
)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Ensure a fresh database for each test."""
    db_path = tmp_path / "test.db"
    # Patch the DB_PATH used by db.py
    import mochi.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()
    yield db_path


class TestMessages:
    def test_save_and_retrieve(self):
        save_message(123, "user", "hello")
        save_message(123, "assistant", "hi there")
        msgs = get_recent_messages(123, limit=10)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_limit(self):
        for i in range(30):
            save_message(1, "user", f"msg {i}")
        msgs = get_recent_messages(1, limit=10)
        assert len(msgs) == 10

    def test_user_isolation(self):
        save_message(1, "user", "from user 1")
        save_message(2, "user", "from user 2")
        msgs = get_recent_messages(1, limit=10)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "from user 1"

    def test_last_user_message_time(self):
        assert get_last_user_message_time(1) is None
        save_message(1, "user", "hey")
        t = get_last_user_message_time(1)
        assert t is not None

    def test_message_count_today(self):
        assert get_message_count_today(1) == 0
        save_message(1, "user", "one")
        save_message(1, "user", "two")
        save_message(1, "assistant", "reply")  # should not count
        assert get_message_count_today(1) == 2


class TestCoreMemory:
    def test_empty(self):
        assert get_core_memory(999) == ""

    def test_set_and_get(self):
        update_core_memory(1, "Likes coffee")
        assert get_core_memory(1) == "Likes coffee"

    def test_upsert(self):
        update_core_memory(1, "v1")
        update_core_memory(1, "v2")
        assert get_core_memory(1) == "v2"


class TestReminders:
    def test_create_and_list(self):
        rid = create_reminder(1, 100, "Take a break", "2020-01-01T12:00:00")
        assert rid > 0
        pending = get_pending_reminders()
        assert any(r["id"] == rid for r in pending)

    def test_fire_reminder(self):
        rid = create_reminder(1, 100, "Test", "2020-01-01T12:00:00")
        mark_reminder_fired(rid)
        pending = get_pending_reminders()
        assert not any(r["id"] == rid for r in pending)

    def test_upcoming_reminders(self):
        # Past reminder (should show as upcoming since it's already due)
        create_reminder(1, 100, "Old timer", "2020-01-01T00:00:00")
        upcoming = get_upcoming_reminders(1, hours_ahead=2)
        assert len(upcoming) >= 1


class TestTodos:
    def test_create_and_list(self):
        tid = create_todo(1, "Buy milk")
        assert tid > 0
        todos = get_todos(1)
        assert len(todos) == 1
        assert todos[0]["task"] == "Buy milk"

    def test_complete(self):
        tid = create_todo(1, "Exercise")
        complete_todo(tid)
        active = get_todos(1, include_done=False)
        assert len(active) == 0
        all_todos = get_todos(1, include_done=True)
        assert len(all_todos) == 1

    def test_active_count(self):
        assert get_active_todo_count(1) == 0
        create_todo(1, "A")
        create_todo(1, "B")
        assert get_active_todo_count(1) == 2
        create_todo(1, "C")
        complete_todo(create_todo(1, "D"))
        assert get_active_todo_count(1) == 3


class TestMemoryItems:
    def test_save_and_get(self):
        save_memory_item(1, "preference", "Likes jasmine tea")
        items = recall_memory(1)
        assert len(items) == 1
        assert "jasmine tea" in items[0]["content"]
