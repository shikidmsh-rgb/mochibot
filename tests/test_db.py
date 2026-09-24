"""SQLite regression tests for conversation context and durable receipts."""

import mochi.db as db
from mochi.db import (
    finish_tool_execution,
    get_recent_messages,
    get_recent_tool_executions,
    save_message,
    set_context_reset,
    start_tool_execution,
)


def test_usage_totals_respect_local_day_and_month(monkeypatch):
    from datetime import datetime, timezone
    from mochi.transport.utils import format_usage_summary

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 23, 12, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(db, "datetime", Clock)
    db.log_usage(100, 20, 120, model="main", reasoning_tokens=5)
    db.log_usage(30, 10, 40, model="lite")
    conn = db._connect()
    conn.execute(
        "INSERT INTO usage_log "
        "(prompt_tokens, completion_tokens, total_tokens, model, created_at) "
        "VALUES (10, 5, 15, 'main', '2026-09-01T01:00:00+00:00'), "
        "(50, 10, 60, 'main', '2026-08-31T23:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    summary = db.get_usage_summary()
    assert summary["today"]["total"] == 160
    assert summary["month"]["total"] == 175
    assert summary["today"]["by_model"]["main"]["reasoning"] == 5
    assert "160 tokens" in format_usage_summary(summary)
    assert "175 tokens" in format_usage_summary(summary)


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
