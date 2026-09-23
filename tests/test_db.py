"""High-value SQLite regression tests.

These tests intentionally cover durable user data and upgrade boundaries, not
every query helper.
"""

import sqlite3

import mochi.db as db
from mochi.db import (
    delete_memory_items,
    finish_tool_execution,
    get_recent_messages,
    get_recent_tool_executions,
    init_db,
    list_memory_trash,
    recall_memory,
    restore_memory_from_trash,
    save_memory_item,
    save_message,
    set_context_reset,
    start_tool_execution,
)
from mochi.skills.reminder.queries import (
    create_reminder,
    get_pending_reminders,
    mark_reminder_fired,
)
from mochi.skills.todo.queries import (
    complete_todo,
    create_todo,
    get_todos,
    update_todo,
)


def test_messages_round_trip_and_stay_isolated_by_user():
    save_message(1, "user", "hello", turn_id="turn_1")
    save_message(1, "assistant", "hi", tool_history='[{"name":"weather"}]')
    save_message(2, "user", "private")

    messages = get_recent_messages(1)

    assert [(m["role"], m["content"]) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi"),
    ]
    assert messages[0]["turn_id"] == "turn_1"
    assert messages[1]["tool_history"] == '[{"name":"weather"}]'


def test_reset_boundary_hides_older_conversation():
    save_message(1, "user", "before reset")
    boundary = set_context_reset(1)
    save_message(1, "user", "after reset")

    assert [m["content"] for m in get_recent_messages(1, since=boundary)] == [
        "after reset"
    ]


def _save_turn(name):
    save_message(1, "user", f"user-{name}", turn_id=name)
    save_message(1, "assistant", f"assistant-{name}", turn_id=name)


def test_context_merges_standalone_deliveries_without_changing_complete_turns():
    _save_turn("old")
    save_message(1, "assistant", "outside-window", processed=True)
    _save_turn("first")
    first = save_message(1, "assistant", "already-reminded", processed=True)
    _save_turn("second")
    second = save_message(1, "assistant", "already-reminded", processed=True)
    save_message(2, "assistant", "other-user", processed=True)
    save_message(1, "user", "unanswered", turn_id="pending")

    context = db.get_conversation_context(1, 2, include_summary=False)

    assert [m["content"] for m in context["recent"]] == [
        "user-first", "assistant-first", "already-reminded",
        "user-second", "assistant-second", "already-reminded",
    ]
    assert [m["id"] for m in context["recent"] if m.get("standalone")] == [
        first, second,
    ]
    assert [m["content"] for m in context["trailing"]] == ["unanswered"]
    assert db.get_conversation_summary_status(1)["pending_turns"] == 3
    _, extraction_messages = db.get_memory_extraction_batch(1, 3)
    assert len(extraction_messages) == 6
    assert all(not message["processed"] for message in extraction_messages)


def test_reasoning_replay_respects_history_window_and_memory_boundaries():
    source = "https://api.deepseek.com/v1::model"
    for name, reasoning in [("old", "Old reasoning"), ("recent", "")]:
        save_message(1, "user", f"user-{name}", turn_id=name)
        save_message(
            1, "assistant", f"assistant-{name}", turn_id=name,
            reasoning_content=reasoning, reasoning_source=source,
        )
    save_message(2, "assistant", "other-user", processed=True,
                 reasoning_content="Private", reasoning_source=source)

    context = db.get_conversation_context(
        1, 1, include_summary=False, reasoning_source=source,
    )
    assert [message["content"] for message in context["recent"]] == [
        "user-recent", "assistant-recent",
    ]
    assert context["recent"][-1]["reasoning_content"] == ""
    _, extraction_messages = db.get_memory_extraction_batch(1, 2)
    assert len(extraction_messages) == 4
    assert all("reasoning_content" not in message for message in extraction_messages)
    claim = db.get_conversation_summary_batch(1, batch_turns=2)
    assert claim is not None
    assert all(
        "reasoning_content" not in turn[role]
        for turn in claim["turns"] for role in ("user", "assistant")
    )



def test_standalone_history_has_separate_count_and_body_limits():
    _save_turn("kept")
    for number in range(7):
        save_message(1, "assistant", f"proactive-{number}", processed=True)
    save_message(1, "assistant", "x" * 5000, processed=True)

    context = db.get_conversation_context(1, 1, include_summary=False)

    assert len(context["recent"]) == 7
    assert [m["content"] for m in context["recent"][:2]] == [
        "user-kept", "assistant-kept",
    ]
    assert [m["content"] for m in context["recent"][2:-1]] == [
        "proactive-3", "proactive-4", "proactive-5", "proactive-6",
    ]
    assert len(context["recent"][-1]["content"]) == 2000
    assert context["recent"][-1]["content"].endswith("...")
    assert get_recent_messages(1)[-1]["content"] == "x" * 5000
    ordinary = db.get_conversation_context(
        1, 1, include_summary=False, include_standalone=False,
    )
    assert len(ordinary["recent"]) == 2


def test_standalone_history_survives_midnight_but_not_context_reset():
    old_id = save_message(1, "assistant", "last-night", processed=True)
    with db._connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = '2020-01-01T23:59:00+00:00' WHERE id = ?",
            (old_id,),
        )
    assert [
        m["content"] for m in db.get_conversation_context(1)["recent"]
    ] == ["last-night"]

    set_context_reset(1)
    save_message(1, "assistant", "new-epoch", processed=True)
    save_message(2, "assistant", "other-user", processed=True)

    assert [
        m["content"] for m in db.get_conversation_context(1)["recent"]
    ] == ["new-epoch"]


def test_standalone_delivery_between_user_and_reply_keeps_chronology():
    save_message(1, "user", "question", turn_id="chat")
    save_message(1, "assistant", "independent-delivery", processed=True)
    save_message(1, "assistant", "answer", turn_id="chat")

    assert [
        m["content"] for m in db.get_conversation_context(1)["recent"]
    ] == ["question", "independent-delivery", "answer"]


def test_tool_ledger_keeps_real_receipt_and_filters_non_changes():
    success_id = start_tool_execution(
        turn_id="turn_1", tool_call_id="call_1", user_id=1,
        source="chat", skill_name="reminder",
        tool_name="manage_reminder", action="create",
        arguments_json='{"message":"report"}',
    )
    finish_tool_execution(
        success_id, status="success", result_summary="Reminder #27 set",
        entity_refs=["reminder:27"], state_changed=True,
    )
    failed_id = start_tool_execution(
        turn_id="turn_2", tool_call_id="call_2", user_id=1,
        source="chat", skill_name="reminder",
        tool_name="manage_reminder", action="create", arguments_json="{}",
    )
    finish_tool_execution(failed_id, status="failed", result_summary="failed")

    rows = get_recent_tool_executions(1, state_changes_only=True)

    assert len(rows) == 1
    assert rows[0]["arguments"] == {"message": "report"}
    assert rows[0]["entity_refs"] == ["reminder:27"]


def test_due_reminder_stops_being_pending_after_delivery():
    reminder_id = create_reminder(1, 100, "Stretch", "2020-01-01T00:00:00")
    assert any(r["id"] == reminder_id for r in get_pending_reminders())

    mark_reminder_fired(reminder_id)

    assert all(r["id"] != reminder_id for r in get_pending_reminders())


def test_todo_lifecycle_respects_user_ownership():
    todo_id = create_todo(1, "Buy milk")
    assert update_todo(2, todo_id, task="Hijacked") is False
    assert update_todo(1, todo_id, task="Buy oat milk") is True
    assert update_todo(1, todo_id, task="Buy oat milk") is False
    assert complete_todo(2, todo_id) is False
    assert complete_todo(1, todo_id) is True
    assert complete_todo(1, todo_id) is False

    todos = get_todos(1, include_done=True)
    assert len(todos) == 1
    assert todos[0]["task"] == "Buy oat milk"
    assert get_todos(1, include_done=False) == []


def test_deleted_memory_can_be_restored():
    first_event = save_memory_item(1, "[2026-08-15] Started a new project")
    second_event = save_memory_item(1, "[2026-08-15] Started learning Japanese")
    assert first_event != second_event

    memory_id = save_memory_item(1, "Likes jasmine tea")
    assert delete_memory_items([memory_id], deleted_by="user") == 1
    assert recall_memory(1, query="jasmine") == []

    trash = list_memory_trash(1)
    assert trash[0]["content"] == "Likes jasmine tea"
    assert restore_memory_from_trash(trash[0]["id"], 1) is not None
    assert recall_memory(1, query="jasmine")[0]["content"] == "Likes jasmine tea"


def test_old_messages_database_upgrades_without_data_loss(tmp_path, monkeypatch):
    import mochi.db as db_module

    db_path = tmp_path / "old-messages.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "role TEXT, content TEXT, created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (1, 1, 'user', 'keep me', '2025-01-01')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    content = conn.execute("SELECT content FROM messages WHERE id = 1").fetchone()[0]
    conn.close()
    assert {"processed", "image_data", "tool_history", "turn_id"} <= columns
    assert content == "keep me"


def test_old_memory_database_upgrades_without_data_loss(tmp_path, monkeypatch):
    import mochi.db as db_module

    db_path = tmp_path / "old-memory.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memory_items (id INTEGER PRIMARY KEY, user_id INTEGER, "
        "category TEXT, content TEXT, importance INTEGER DEFAULT 1, "
        "source TEXT, processed INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO memory_items (id, user_id, category, content) "
        "VALUES (1, 1, 'fact', 'keep this memory')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    init_db()

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_items)")}
    content = conn.execute("SELECT content FROM memory_items WHERE id = 1").fetchone()[0]
    conn.close()
    assert {"embedding", "access_count", "last_accessed"} <= columns
    assert content == "keep this memory"
