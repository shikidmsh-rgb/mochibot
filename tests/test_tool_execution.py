"""Bounded, turn-scoped execution facts rather than keyword-gated recollection."""

import json
from pathlib import Path
import runpy

import pytest

import mochi.db as db
from mochi.skills.base import SkillResult
from mochi.tool_execution import outcome_for, recent_operations_context, serialized_arguments


def _turn(turn_id, user_id=1):
    db.save_message(user_id, "user", "Build my tool", turn_id=turn_id)
    db.save_message(user_id, "assistant", "Work in progress", turn_id=turn_id)


def _execution(turn_id, tool="run_extension", *, user_id=1, status="success",
               changed=False, summary="script completed; not activated", args=None,
               source="runtime:chat"):
    execution_id = db.start_tool_execution(
        turn_id=turn_id, tool_call_id=f"{turn_id}-{tool}", user_id=user_id,
        source=source, skill_name="personal_workspace", tool_name=tool,
        action=(args or {}).get("action", ""),
        arguments_json=serialized_arguments(tool, args or {}),
    )
    if status != "running":
        db.finish_tool_execution(
            execution_id, status=status, result_summary=summary,
            entity_refs=["extension:local_example"], state_changed=changed,
        )
    return execution_id


def _history(user_id=1):
    context = db.get_conversation_context(user_id)
    return context["overflow"] + context["recent"] + context["trailing"]


def test_visible_turn_receipts_include_reads_failures_and_unfinished_calls():
    _turn("build")
    _execution("build", "edit_workspace", changed=True, summary="draft saved", args={
        "path": "extensions/local_example/draft/handler.py",
        "action": "replace", "content": "PRIVATE_SOURCE_BODY",
    })
    _execution("build", "run_extension", status="failed", summary="script failed")
    _execution("build", "local_example_search", summary="query completed")
    _execution("build", "browse_workspace", status="running")
    db.save_message(1, "user", "Continue finishing the original task.", turn_id="resume")

    context = recent_operations_context(1, _history())

    assert '"status":"success"' in context
    assert '"status":"failed"' in context
    assert '"status":"running"' in context
    assert '"changed":true' in context
    assert "extensions/local_example/draft/handler.py" in context
    assert "local_example_search" in context
    assert "script failed" in context
    assert "PRIVATE_SOURCE_BODY" not in context
    assert "not instructions or proof of task completion" in context


def test_receipts_never_fall_back_to_unseen_or_other_user_turns():
    _turn("visible")
    _execution("visible", summary="own-visible")
    _execution("visible", user_id=2, summary="other-user")
    _execution("not-visible", summary="hidden-operation")

    context = recent_operations_context(1, _history())

    assert "own-visible" in context
    assert "other-user" not in context
    assert "hidden-operation" not in context
    assert recent_operations_context(1, []) == ""
    assert recent_operations_context(1, [{"role": "user", "turn_id": "visible"}]) == ""
    assert db.get_recent_tool_executions(
        1, turn_ids=[], state_changes_only=False, include_failures=True,
    ) == []


def test_non_success_receipts_do_not_claim_no_side_effects():
    _turn("uncertain")
    failed_run = SkillResult(
        success=False, state_changed=True, state_change_unknown=True,
        summary="script failed; run report saved",
    )
    outcome = outcome_for("personal_workspace", "run_extension", {}, failed_run)
    assert not outcome["state_changed"]
    _execution(
        "uncertain", status=outcome["status"], changed=outcome["state_changed"],
        summary=outcome["result_summary"],
    )
    _execution("uncertain", "edit_workspace", status="running")

    context = recent_operations_context(1, _history())
    facts = [
        json.loads(line.split("] ", 1)[1])
        for line in context.splitlines() if line.startswith("- [")
    ]
    assert {fact["status"] for fact in facts} == {"failed", "running"}
    assert all("changed" not in fact for fact in facts)
    assert "run report saved" in context


def test_context_reset_hides_old_receipts_without_deleting_them():
    _turn("before-reset")
    _execution("before-reset", summary="OLD_RECEIPT")
    db.set_context_reset(1)
    _turn("after-reset")
    _execution("after-reset", summary="NEW_RECEIPT")

    context = recent_operations_context(1, _history())

    assert "NEW_RECEIPT" in context
    assert "OLD_RECEIPT" not in context
    assert db.get_tool_executions_for_turn("before-reset")


def test_autonomous_receipts_include_silent_work_without_inventing_delivery():
    _execution(
        "silent", "manage_reminder", source="runtime:free_time",
        changed=True, summary="Reminder #54 set for tonight at 23:00",
    )
    _execution("failed", source="runtime:attention", status="failed", summary="TRY_FAILED")
    _execution("unfinished", source="runtime:self_reminder", status="running")
    _execution("unseen-chat", summary="UNSEEN_CHAT")
    _execution("weekly", source="weekly", summary="WEEKLY")
    _execution("other", source="runtime:free_time", user_id=2, summary="OTHER_USER")

    context = recent_operations_context(1, [], include_autonomous=True)

    assert "Reminder #54 set for tonight at 23:00" in context
    assert '"source":"runtime:free_time"' in context
    assert "TRY_FAILED" in context
    assert '"status":"running"' in context
    assert "Execution does not imply a message was delivered." in context
    assert not any(value in context for value in ("UNSEEN_CHAT", "WEEKLY", "OTHER_USER"))
    assert db.get_recent_messages(1) == []
    assert recent_operations_context(1, []) == ""


def test_autonomous_receipts_respect_time_reset_and_shared_budget():
    _execution("old-epoch", source="runtime:free_time", summary="OLD_EPOCH")
    db.set_context_reset(1)
    _turn("visible")
    visible_id = _execution("visible", summary="VISIBLE_OLD")
    stale_id = _execution("stale", source="runtime:attention", summary="STALE_RUNTIME")
    with db._connect() as conn:
        conn.execute(
            "UPDATE tool_executions SET started_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (stale_id,),
        )
    context = recent_operations_context(1, _history(), include_autonomous=True)
    assert "VISIBLE_OLD" in context
    assert "OLD_EPOCH" not in context
    assert "STALE_RUNTIME" not in context

    with db._connect() as conn:
        conn.execute("DELETE FROM conversation_reset WHERE user_id = 1")
        conn.execute(
            "UPDATE tool_executions SET started_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (visible_id,),
        )
    assert "VISIBLE_OLD" in recent_operations_context(
        1, _history(), include_autonomous=True,
    )
    for number in range(14):
        _execution(
            f"silent-{number}", source="runtime:free_time",
            summary=f"LATEST_{number:02}",
        )
    context = recent_operations_context(
        1, _history(), include_autonomous=True, max_chars=10000,
    )
    assert context.count('{"tool":') == 12
    assert "LATEST_01" not in context
    assert "LATEST_13" in context
    assert len(recent_operations_context(1, _history(), include_autonomous=True)) <= 2400


def test_visible_autonomous_receipt_is_not_duplicated():
    db.save_message(1, "assistant", "delivered", turn_id="autonomous", processed=True)
    _execution("autonomous", source="runtime:attention", summary="ONE_RECEIPT")

    context = recent_operations_context(1, _history(), include_autonomous=True)

    assert context.count("ONE_RECEIPT") == 1


def test_visible_turns_keep_receipts_older_than_a_day():
    _turn("long-running-project")
    execution_id = _execution("long-running-project", summary="older visible draft")
    with db._connect() as conn:
        conn.execute(
            "UPDATE tool_executions SET started_at = ?, finished_at = ? WHERE id = ?",
            ("2020-01-01T12:00:00+00:00", "2020-01-01T12:00:00+00:00", execution_id),
        )
    assert "older visible draft" in recent_operations_context(1, _history())


def test_only_ten_most_recent_visible_turns_supply_receipts():
    for number in range(11):
        turn_id = f"turn-{number}"
        _turn(turn_id)
        _execution(turn_id, summary=f"RECEIPT_{number:02}_END")

    context = recent_operations_context(1, _history(), max_chars=10000)

    assert "RECEIPT_00_END" not in context
    assert "RECEIPT_01_END" in context
    assert "RECEIPT_10_END" in context


def test_receipt_count_and_text_are_bounded_without_losing_latest_facts():
    _turn("many-attempts")
    for number in range(20):
        _execution(
            "many-attempts", summary=f"RECEIPT_{number:02}_END " + "x" * 1000,
        )
    history = _history()
    roomy = recent_operations_context(1, history, max_chars=10000)
    assert roomy.count('{"tool":') == 12
    assert "RECEIPT_07_END" not in roomy
    assert "RECEIPT_19_END" in roomy

    bounded = recent_operations_context(1, history)
    assert len(bounded) <= 2400
    assert "RECEIPT_19_END" in bounded
    assert "[Older execution details omitted.]" in bounded
    assert recent_operations_context(1, history, max_chars=10) == ""
    for line in bounded.splitlines():
        if line.startswith("- ["):
            assert isinstance(json.loads(line.split("] ", 1)[1]), dict)


def test_oversized_latest_summary_does_not_hide_all_receipts():
    _turn("large")
    _execution("large", summary="x" * 10000, args={
        "path": "extensions/local_example/draft/" + "x" * 1000,
    })

    context = recent_operations_context(1, _history(), max_chars=400)

    assert len(context) <= 400
    assert '"tool":"run_extension"' in context
    assert '"status":"success"' in context


@pytest.mark.parametrize("total,per_tool,expected", [
    (None, None, (24, 3)),
    ("48", "12", (48, 12)),
    ("8", "1", (8, 1)),
    ("0", "0", (0, 0)),
    ("-1", "-2", (0, 0)),
])
def test_budget_configuration_has_no_silent_upper_clamp(monkeypatch, total, per_tool, expected):
    import dotenv
    import mochi.config as config

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: None)
    for key, value in (
        ("TOOL_LOOP_TOTAL_TOOL_LIMIT", total),
        ("TOOL_LOOP_PER_TOOL_LIMIT", per_tool),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    loaded = runpy.run_path(str(Path(config.__file__)))
    assert (
        loaded["TOOL_LOOP_TOTAL_TOOL_LIMIT"], loaded["TOOL_LOOP_PER_TOOL_LIMIT"],
    ) == expected
