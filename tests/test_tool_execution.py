"""Bounded, turn-scoped execution facts rather than keyword-gated recollection."""

import json
from pathlib import Path
import runpy

import pytest

import mochi.db as db
from mochi.skills.base import SkillResult
from mochi.tool_execution import outcome_for, recent_operations_context, serialized_arguments


def test_skill_schema_preserves_name_and_validates_nested_food_items():
    from mochi.skills.base import _build_tool_schema, _parse_param_table
    from mochi.tool_availability import ToolAvailability

    params, required = _parse_param_table("""
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| name | string | yes | Entry name |
| items | array (items: object {name:string, calories:integer, protein_g:number, carbs_g:number, fat_g:number}) | yes | Food estimates |
""")
    availability = ToolAvailability.from_definitions(
        [_build_tool_schema("record_food", "Record food", params, required)],
        source="test",
    )
    food = {"name": "rice", "calories": 100, "protein_g": 2.5, "carbs_g": 20, "fat_g": 1}
    assert availability.validate_arguments(
        "record_food", {"name": "lunch", "items": [food]},
    ) is None
    for arguments in (
        {"items": [food]},
        {"name": "lunch", "items": json.dumps([food])},
        {"name": "lunch", "items": [{"name": "rice"}]},
        {"name": "lunch", "items": [{**food, "calories": True}]},
        {"name": "lunch", "items": [{**food, "protein_g": float("inf")}]},
        {"name": "lunch", "items": [{**food, "unexpected": 1}]},
    ):
        assert availability.validate_arguments("record_food", arguments) is not None


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
    _execution(
        "bedtime", "schedule_self_reminder", source="runtime:bedtime",
        changed=True, summary="Reminder #60 set for tomorrow at 23:00",
    )
    _execution("unseen-chat", summary="UNSEEN_CHAT")
    _execution("weekly", source="weekly", summary="WEEKLY")
    _execution("other", source="runtime:bedtime", user_id=2, summary="OTHER_USER")

    context = recent_operations_context(1, [], include_autonomous=True)

    assert "Reminder #54 set for tonight at 23:00" in context
    assert '"source":"runtime:free_time"' in context
    assert "Reminder #60 set for tomorrow at 23:00" in context
    assert '"source":"runtime:bedtime"' in context
    assert "TRY_FAILED" in context
    assert '"status":"running"' in context
    assert not any(value in context for value in ("UNSEEN_CHAT", "WEEKLY", "OTHER_USER"))
    assert db.get_recent_messages(1) == []
    assert recent_operations_context(1, []) == ""


@pytest.mark.parametrize("source", ["runtime:free_time", "runtime:bedtime"])
def test_autonomous_receipts_respect_time_reset_and_shared_budget(source):
    _execution("old-epoch", source=source, summary="OLD_EPOCH")
    db.set_context_reset(1)
    _turn("visible")
    visible_id = _execution("visible", summary="VISIBLE_OLD")
    stale_id = _execution("stale", source=source, summary="STALE_RUNTIME")
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
            f"silent-{number}", source=source,
            summary=f"LATEST_{number:02}",
        )
    context = recent_operations_context(
        1, _history(), include_autonomous=True, max_chars=10000,
    )
    assert context.count('{"tool":') == 12
    assert "LATEST_01" not in context
    assert "LATEST_13" in context
    assert len(recent_operations_context(1, _history(), include_autonomous=True)) <= 2400


def test_visible_turns_keep_receipts_older_than_a_day():
    _turn("long-running-project")
    execution_id = _execution("long-running-project", summary="older visible draft")
    with db._connect() as conn:
        conn.execute(
            "UPDATE tool_executions SET started_at = ?, finished_at = ? WHERE id = ?",
            ("2020-01-01T12:00:00+00:00", "2020-01-01T12:00:00+00:00", execution_id),
        )
    assert "older visible draft" in recent_operations_context(1, _history())


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


@pytest.mark.parametrize("total,per_tool,expected", [
    ("48", "12", (48, 12)),
    ("0", "0", (0, 0)),
])
def test_budget_configuration_has_no_silent_upper_clamp(monkeypatch, total, per_tool, expected):
    import dotenv
    import mochi.config as config

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: None)
    for key, value in (
        ("TOOL_LOOP_TOTAL_TOOL_LIMIT", total),
        ("TOOL_LOOP_PER_TOOL_LIMIT", per_tool),
    ):
        monkeypatch.setenv(key, value)
    loaded = runpy.run_path(str(Path(config.__file__)))
    assert (
        loaded["TOOL_LOOP_TOTAL_TOOL_LIMIT"], loaded["TOOL_LOOP_PER_TOOL_LIMIT"],
    ) == expected


def test_tool_budget_counts_identical_arguments_not_distinct_operations():
    from mochi.request_tools import ToolLoopBudget

    budget = ToolLoopBudget()
    for index in range(5):
        assert budget.claim_tool(
            "manage_todo", {"action": "update", "todo_id": index},
            total_limit=10, per_tool_limit=2,
        ) is None
    args = {"todo_id": 0, "action": "update"}
    assert budget.claim_tool(
        "manage_todo", args, total_limit=10, per_tool_limit=2,
    ) is None
    denied = budget.claim_tool(
        "manage_todo", args, total_limit=10, per_tool_limit=2,
    )
    assert denied["code"] == "repeated_tool_call"
    assert denied["started"] is False
    assert denied["changed"] is False
    assert budget.claim_tool(
        "manage_todo", {"todo_id": 6}, total_limit=6, per_tool_limit=2,
    )["code"] == "tool_call_limit_reached"


def test_tool_results_report_real_outcome(monkeypatch):
    import asyncio

    from mochi.skills.base import Skill, SkillContext
    from mochi.tool_availability import ToolAvailability, tool_call_error
    from mochi.tool_execution import model_result_for

    availability = ToolAvailability.from_definitions([{
        "type": "function",
        "function": {
            "name": "nullable",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": ["integer", "null"]}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }], source="test")
    assert availability.validate_arguments("nullable", {"value": None}) is None
    assert availability.validate_arguments("nullable", {"value": "wrong"})

    assert json.loads(tool_call_error(
        "manage_todo", "invalid_tool_arguments", "arguments.todo_id is required",
    )) == {
        "ok": False, "code": "invalid_tool_arguments", "started": False,
        "retryable": True, "changed": False,
        "message": "arguments.todo_id is required",
    }

    class _SemanticFailureSkill(Skill):
        async def execute(self, context):
            return SkillResult(
                output="todo_id is required", success=False,
                error_code="invalid_arguments", retryable=True,
            )

    semantic = asyncio.run(_SemanticFailureSkill().run(SkillContext(
        trigger="tool_call", tool_name="manage_todo",
    )))
    assert json.loads(model_result_for(semantic)) == {
        "ok": False, "code": "invalid_arguments", "started": True,
        "retryable": True, "changed": False, "message": "todo_id is required",
    }

    class _ExplodingSkill(Skill):
        async def execute(self, context):
            raise RuntimeError("write outcome unknown")

    uncertain = json.loads(model_result_for(asyncio.run(_ExplodingSkill().run(
        SkillContext(trigger="tool_call", tool_name="example_write"),
    ))))
    assert uncertain["ok"] is False and uncertain["started"] is True
    assert "changed" not in uncertain

    mutation = SkillResult(
        output="Error-shaped prose is still only prose.",
        state_changed=True, execution_started=True,
    )
    assert json.loads(model_result_for(mutation)) == {
        "ok": True, "changed": True,
        "result": "Error-shaped prose is still only prose.",
    }
    assert outcome_for("example", "example_write", {}, mutation)["state_changed"] is True

    import mochi.skills.web_search.handler as web_handler

    async def _search(*args, **kwargs):
        return "1. External result"

    async def _failed_search(*args, **kwargs):
        raise RuntimeError("network unavailable")

    context = SkillContext(
        trigger="tool_call", tool_name="web_search", args={"query": "Mochi"},
    )
    monkeypatch.setattr(web_handler, "_bing_search", _search)
    web = json.loads(model_result_for(asyncio.run(
        web_handler.WebSearchSkill().run(context),
    )))
    assert web["source"] == "external_web" and web["authority"] == "untrusted_data"
    monkeypatch.setattr(web_handler, "_bing_search", _failed_search)
    failed = json.loads(model_result_for(asyncio.run(
        web_handler.WebSearchSkill().run(context),
    )))
    assert failed["ok"] is False
    assert "source" not in failed and "authority" not in failed
