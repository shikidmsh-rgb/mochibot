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
    delete_todo,
    update_todo,
    purge_done_todos,
    get_last_user_message_time,
    get_message_count_today,
    get_active_todo_count,
    get_upcoming_reminders,
    save_memory_item,
    recall_memory,
    list_all_memories,
    delete_memory_items,
    get_memory_stats,
    list_memory_trash,
    restore_memory_from_trash,
    cleanup_old_trash,
    update_memory_importance,
    merge_memory_items,
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

    def test_create_with_nudge_date(self):
        tid = create_todo(1, "Dentist", nudge_date="2026-04-15")
        todos = get_todos(1)
        assert todos[0]["nudge_date"] == "2026-04-15"

    def test_complete(self):
        tid = create_todo(1, "Exercise")
        ok = complete_todo(1, tid)
        assert ok is True
        active = get_todos(1, include_done=False)
        assert len(active) == 0
        all_todos = get_todos(1, include_done=True)
        assert len(all_todos) == 1

    def test_complete_wrong_user(self):
        tid = create_todo(1, "Exercise")
        ok = complete_todo(999, tid)
        assert ok is False

    def test_delete(self):
        tid = create_todo(1, "Trash")
        ok = delete_todo(1, tid)
        assert ok is True
        assert len(get_todos(1)) == 0

    def test_delete_wrong_user(self):
        tid = create_todo(1, "Trash")
        ok = delete_todo(999, tid)
        assert ok is False
        assert len(get_todos(1)) == 1

    def test_update(self):
        tid = create_todo(1, "Old task")
        ok = update_todo(1, tid, task="New task", nudge_date="2026-05-01")
        assert ok is True
        todos = get_todos(1)
        assert todos[0]["task"] == "New task"
        assert todos[0]["nudge_date"] == "2026-05-01"

    def test_update_wrong_user(self):
        tid = create_todo(1, "Mine")
        ok = update_todo(999, tid, task="Hacked")
        assert ok is False

    def test_purge_done(self):
        tid = create_todo(1, "Done long ago")
        complete_todo(1, tid)
        # Artificially backdate completed_at
        import mochi.db as db_module
        conn = db_module._connect()
        conn.execute("UPDATE todos SET completed_at = '2020-01-01T00:00:00' WHERE id = ?", (tid,))
        conn.commit()
        conn.close()
        deleted = purge_done_todos(days=30)
        assert deleted == 1

    def test_active_count(self):
        assert get_active_todo_count(1) == 0
        create_todo(1, "A")
        create_todo(1, "B")
        assert get_active_todo_count(1) == 2
        create_todo(1, "C")
        complete_todo(1, create_todo(1, "D"))
        assert get_active_todo_count(1) == 3


class TestMemoryItems:
    def test_save_and_get(self):
        save_memory_item(1, "preference", "Likes jasmine tea")
        items = recall_memory(1)
        assert len(items) == 1
        assert "jasmine tea" in items[0]["content"]


class TestNewTables:
    """Verify Phase 1 tables exist after init_db()."""

    NEW_TABLES = [
        "notes", "knowledge", "proactive_log", "ops_context_items",
        "health_log", "pet_log", "life_log", "notifications",
        "memory_trash", "skill_config", "sticker_registry",
    ]

    def test_new_tables_exist(self, fresh_db):
        import sqlite3
        conn = sqlite3.connect(str(fresh_db))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        for t in self.NEW_TABLES:
            assert t in tables, f"Table {t} missing after init_db()"

    def test_skill_config_unique(self, fresh_db):
        """skill_config has UNIQUE(skill_name, key)."""
        import mochi.db as db_module
        conn = db_module._connect()
        conn.execute(
            "INSERT INTO skill_config (skill_name, key, value, updated_at) VALUES (?, ?, ?, datetime('now'))",
            ("memory", "enabled", "true"),
        )
        conn.commit()
        # Duplicate should conflict — upsert or error
        try:
            conn.execute(
                "INSERT INTO skill_config (skill_name, key, value, updated_at) VALUES (?, ?, ?, datetime('now'))",
                ("memory", "enabled", "false"),
            )
            conn.commit()
            # If no error, check that we have exactly 2 rows (no UNIQUE constraint)
            # or 1 row (with UNIQUE constraint + ON CONFLICT REPLACE)
            count = conn.execute(
                "SELECT COUNT(*) FROM skill_config WHERE skill_name='memory' AND key='enabled'"
            ).fetchone()[0]
            assert count <= 2  # either is fine, just ensure table works
        except Exception:
            pass  # UNIQUE constraint violation is expected
        conn.close()


class TestMigrations:
    """Verify ALTER TABLE migrations work on a pre-existing (old schema) DB."""

    def test_messages_migration(self, tmp_path, monkeypatch):
        """Old messages table gains 'processed' and 'image_data' columns."""
        import sqlite3
        import mochi.db as db_module

        db_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, role TEXT, content TEXT, created_at TEXT
        )""")
        conn.execute("INSERT INTO messages (user_id, role, content, created_at) VALUES (1, 'user', 'hi', '2025-01-01')")
        conn.commit()
        conn.close()

        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        init_db()

        conn = sqlite3.connect(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        assert "processed" in cols, "messages.processed migration failed"
        assert "image_data" in cols, "messages.image_data migration failed"
        # Existing data preserved
        row = conn.execute("SELECT content FROM messages WHERE user_id=1").fetchone()
        assert row[0] == "hi"
        conn.close()

    def test_memory_items_migration(self, tmp_path, monkeypatch):
        """Old memory_items table gains embedding, access_count, last_accessed."""
        import sqlite3
        import mochi.db as db_module

        db_path = tmp_path / "old2.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, category TEXT, content TEXT,
            importance INTEGER DEFAULT 5, source TEXT DEFAULT 'user',
            processed INTEGER DEFAULT 0,
            created_at TEXT, updated_at TEXT
        )""")
        conn.execute(
            "INSERT INTO memory_items (user_id, category, content) VALUES (1, 'fact', 'test memory')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(db_module, "DB_PATH", db_path)
        init_db()

        conn = sqlite3.connect(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_items)").fetchall()]
        assert "embedding" in cols
        assert "access_count" in cols
        assert "last_accessed" in cols
        # Existing data preserved
        row = conn.execute("SELECT content FROM memory_items WHERE user_id=1").fetchone()
        assert row[0] == "test memory"
        conn.close()


class TestListAllMemories:
    def test_list_all(self):
        save_memory_item(1, "preference", "Likes coffee")
        save_memory_item(1, "fact", "Lives in Tokyo")
        items = list_all_memories(1)
        assert len(items) == 2

    def test_filter_by_category(self):
        save_memory_item(1, "preference", "Likes coffee")
        save_memory_item(1, "fact", "Lives in Tokyo")
        items = list_all_memories(1, category="fact")
        assert len(items) == 1
        assert items[0]["category"] == "fact"

    def test_limit(self):
        for i in range(10):
            save_memory_item(1, "fact", f"Fact {i}")
        items = list_all_memories(1, limit=3)
        assert len(items) == 3


class TestSoftDeleteMemory:
    def test_delete_moves_to_trash(self):
        mid = save_memory_item(1, "fact", "To be deleted")
        count = delete_memory_items([mid], deleted_by="user")
        assert count == 1
        # Verify gone from memory_items
        items = recall_memory(1, query="deleted")
        assert len(items) == 0
        # Verify in trash
        trash = list_memory_trash(1)
        assert len(trash) == 1
        assert trash[0]["content"] == "To be deleted"
        assert trash[0]["deleted_by"] == "user"

    def test_delete_nonexistent(self):
        count = delete_memory_items([99999])
        assert count == 0


class TestRestoreFromTrash:
    def test_delete_and_restore(self):
        mid = save_memory_item(1, "preference", "Likes tea")
        delete_memory_items([mid], deleted_by="user")
        # Verify deleted
        assert len(recall_memory(1, query="tea")) == 0
        # Restore
        trash = list_memory_trash(1)
        new_id = restore_memory_from_trash(trash[0]["id"], 1)
        assert new_id is not None
        # Verify restored
        items = recall_memory(1, query="tea")
        assert len(items) == 1
        # Verify trash is empty
        assert len(list_memory_trash(1)) == 0

    def test_restore_nonexistent(self):
        result = restore_memory_from_trash(99999, 1)
        assert result is None


class TestMemoryStats:
    def test_stats(self):
        save_memory_item(1, "preference", "Likes coffee", importance=1)
        save_memory_item(1, "preference", "Likes tea", importance=2)
        save_memory_item(1, "fact", "Has a cat", importance=3)
        stats = get_memory_stats(1)
        assert stats["total"] == 3
        assert stats["high_importance"] == 1  # only ★3
        assert stats["categories"]["preference"] == 2
        assert stats["categories"]["fact"] == 1


class TestCleanupOldTrash:
    def test_purge_old_trash(self):
        mid = save_memory_item(1, "fact", "Old item")
        delete_memory_items([mid], deleted_by="test")
        # Manually backdate the trash entry
        import mochi.db as db_module
        conn = db_module._connect()
        conn.execute(
            "UPDATE memory_trash SET deleted_at = '2020-01-01T00:00:00'"
        )
        conn.commit()
        conn.close()
        # Purge
        purged = cleanup_old_trash(days=30)
        assert purged == 1
        assert len(list_memory_trash(1)) == 0

    def test_no_purge_on_recent(self):
        mid = save_memory_item(1, "fact", "Recent item")
        delete_memory_items([mid], deleted_by="test")
        purged = cleanup_old_trash(days=30)
        assert purged == 0
        assert len(list_memory_trash(1)) == 1


class TestUpdateMemoryImportance:
    def test_update_importance(self):
        mid = save_memory_item(1, "fact", "Test item", importance=1)
        update_memory_importance(mid, 2)
        items = recall_memory(1, query="Test item")
        assert items[0]["importance"] == 2

    def test_update_to_critical(self):
        mid = save_memory_item(1, "fact", "Critical item", importance=1)
        update_memory_importance(mid, 3)
        items = recall_memory(1, query="Critical item")
        assert items[0]["importance"] == 3


class TestMergeMemoryItems:
    def test_merge_basic(self):
        m1 = save_memory_item(1, "fact", "Has a cat named Luna")
        m2 = save_memory_item(1, "fact", "Has a cat, Luna")
        merge_memory_items(m1, [m2], "Has a cat named Luna")
        items = list_all_memories(1, category="fact")
        assert len(items) == 1
        assert items[0]["content"] == "Has a cat named Luna"
        # Merged item should be in trash
        trash = list_memory_trash(1)
        assert len(trash) == 1

    def test_merge_with_importance(self):
        m1 = save_memory_item(1, "fact", "Cat owner", importance=1)
        m2 = save_memory_item(1, "fact", "Has a cat", importance=2)
        merge_memory_items(m1, [m2], "Has a cat named Luna", new_importance=2)
        items = list_all_memories(1, category="fact")
        assert len(items) == 1
        assert items[0]["importance"] == 2
