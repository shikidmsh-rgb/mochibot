"""Bounded, turn-scoped execution facts rather than keyword-gated recollection."""

import json
from pathlib import Path
import runpy

import pytest

import mochi.db as db
from mochi.skills.base import SkillResult
from mochi.tool_execution import (
    RESULT_PAGE_CHARS, RESULT_RETAINED_CHARS, model_result_for, outcome_for,
    read_tool_result, recent_operations_context, retained_result_for, serialized_arguments,
)


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
               source="chat", result=None):
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
            result_json=retained_result_for(tool, result) if result is not None else None,
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


def test_undelivered_chat_receipts_survive_without_assistant_history():
    db.save_message(1, "user", "Save the draft", turn_id="unfinished")
    _execution("unfinished", "edit_workspace", changed=True, summary="Draft saved")
    _execution("unfinished", "browse_workspace", summary="Draft inspected")
    _execution("unfinished", "run_extension", status="failed", summary="Script failed")
    _execution("unfinished", "activate_extension", status="running")
    db.save_message(2, "user", "Other request", turn_id="unfinished")
    _execution("unfinished", user_id=2, summary="OTHER_USER")
    _execution("no-input", summary="NO_OWNER_INPUT")
    db.save_message(2, "user", "Not this owner", turn_id="wrong-input")
    _execution("wrong-input", summary="WRONG_OWNER_INPUT")
    _turn("delivered-but-not-visible")
    _execution("delivered-but-not-visible", summary="UNSEEN_DELIVERED")
    db.save_message(1, "user", "Continue", turn_id="next")

    context = recent_operations_context(1, [])
    facts = [
        json.loads(line.split("] ", 1)[1])
        for line in context.splitlines() if line.startswith("- [")
    ]
    assert {fact["tool"]: fact["status"] for fact in facts} == {
        "edit_workspace": "success", "browse_workspace": "success",
        "run_extension": "failed", "activate_extension": "running",
    }
    assert next(fact for fact in facts if fact["tool"] == "edit_workspace")["changed"] is True
    assert all("changed" not in fact for fact in facts if fact["status"] != "success")
    assert "Draft saved" in context and "Script failed" in context
    assert not any(value in context for value in (
        "OTHER_USER", "NO_OWNER_INPUT", "WRONG_OWNER_INPUT", "UNSEEN_DELIVERED",
    ))
    assert all(
        message["role"] == "user"
        for message in db.get_recent_messages(1) if message["turn_id"] == "unfinished"
    )


def test_undelivered_chat_receipts_respect_reset_and_expiry():
    db.save_message(1, "user", "Expired request", turn_id="expired")
    expired_id = _execution("expired", summary="EXPIRED_RECEIPT")
    with db._connect() as conn:
        conn.execute(
            "UPDATE tool_executions SET started_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (expired_id,),
        )
    assert recent_operations_context(1, []) == ""

    db.save_message(1, "user", "Old request", turn_id="before-reset")
    _execution("before-reset", summary="OLD_RECEIPT")
    db.set_context_reset(1)
    _execution("before-reset", "edit_workspace", summary="LATE_OLD_RECEIPT")
    db.save_message(1, "user", "New request", turn_id="after-reset")
    _execution("after-reset", summary="CURRENT_RECEIPT")

    context = recent_operations_context(1, [])

    assert "CURRENT_RECEIPT" in context
    assert "OLD_RECEIPT" not in context
    assert "EXPIRED_RECEIPT" not in context
    assert len(db.get_tool_executions_for_turn("before-reset")) == 2


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
    db.save_message(1, "user", "Continue working", turn_id="undelivered")
    for number in range(20):
        _execution(
            "many-attempts" if number % 2 == 0 else "undelivered",
            summary=f"RECEIPT_{number:02}_END " + "x" * 1000,
            result=SkillResult(output="DETAILS_NOT_IN_DEFAULT_CONTEXT" * 500),
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
    assert "DETAILS_NOT_IN_DEFAULT_CONTEXT" not in bounded
    assert recent_operations_context(1, history, max_chars=10) == ""
    for line in bounded.splitlines():
        if line.startswith("- ["):
            assert isinstance(json.loads(line.split("] ", 1)[1]), dict)


def test_retained_results_are_redacted_without_changing_the_immediate_result(monkeypatch):
    import mochi.runtime_trace as trace

    monkeypatch.setattr(trace, "_secrets", {"known-test-credential"})
    output = json.dumps({
        "url": "https://example.test/second",
        "token": "nested-secret",
        "text": "known-test-credential",
        "media": "data:image/png;base64,QUJD",
    })
    result = SkillResult(output=output, content_source="external_web")
    before = model_result_for(result)
    _turn("search")
    receipt_id = _execution("search", "web_search", result=result)
    assert model_result_for(result) == before
    row = db.get_tool_execution_result(1, receipt_id)
    assert not any(secret in row["result_json"] for secret in (
        "nested-secret", "known-test-credential", "QUJD",
    ))

    context = recent_operations_context(1, _history())
    fact = json.loads(next(line.split("] ", 1)[1] for line in context.splitlines() if line.startswith("- [")))
    assert fact["receipt_id"] == receipt_id
    assert "https://example.test/second" not in context
    expanded = read_tool_result(1, receipt_id)
    page = json.loads(expanded.output)
    assert json.loads(page["content"])["url"] == "https://example.test/second"
    assert page["redacted"] and page["source_completeness"] == "full"
    assert page["execution"]["ok"] and not expanded.state_changed
    assert json.loads(model_result_for(expanded))["authority"] == "untrusted_data"
    assert retained_result_for("read_tool_result", expanded) is None


def test_retained_result_pages_distinguish_storage_loss_from_unread_content():
    _turn("long-result")
    output = "".join(f"line {index:05d}\n" for index in range(2000))
    receipt_id = _execution("long-result", result=SkillResult(output=output))
    pages, offset = [], 0
    while True:
        result = read_tool_result(1, receipt_id, offset)
        assert result.success
        page = json.loads(result.output)
        assert len(page["content"]) <= RESULT_PAGE_CHARS
        assert page["source_completeness"] == "reduced"
        assert page["total_chars"] == RESULT_RETAINED_CHARS
        pages.append(page["content"])
        if not page["truncated"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] == offset + len(page["content"])
        offset = page["next_offset"]
    assert "".join(pages) == output[:RESULT_RETAINED_CHARS]
    assert not read_tool_result(1, receipt_id, RESULT_RETAINED_CHARS).success
    assert not read_tool_result(1, receipt_id, -1).success
    assert not read_tool_result(1, True).success


def test_receipt_reads_recheck_owner_and_reset_even_for_unfinished_chat():
    db.save_message(1, "user", "Search", turn_id="unfinished")
    result = SkillResult(output="retained observation\n" * 300)
    receipt_id = _execution("unfinished", result=result)
    first = json.loads(read_tool_result(1, receipt_id).output)
    assert first["next_offset"] == RESULT_PAGE_CHARS
    assert not read_tool_result(2, receipt_id).success
    legacy_id = _execution("unfinished", "browse_workspace")
    running_id = _execution("unfinished", "activate_extension", status="running")
    orphan_id = _execution("orphan", result=result)
    weekly_id = _execution("weekly", source="weekly", result=result)
    for inaccessible in (legacy_id, running_id, orphan_id, weekly_id):
        assert not read_tool_result(1, inaccessible).success
    assert "receipt_id" not in json.loads(next(
        line.split("] ", 1)[1] for line in recent_operations_context(1, []).splitlines()
        if '"tool":"browse_workspace"' in line
    ))
    db.set_context_reset(1)
    assert not read_tool_result(1, receipt_id, first["next_offset"]).success
    late_id = _execution("unfinished", "edit_workspace", result=result)
    assert not read_tool_result(1, late_id).success
    autonomous_id = _execution("new-epoch", source="runtime:free_time", result=result)
    assert read_tool_result(1, autonomous_id).success
    assert db.get_tool_executions_for_turn("unfinished")


@pytest.mark.asyncio
async def test_read_tool_loads_on_demand_and_preserves_uncertain_execution():
    import mochi.skills as registry
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    _, definitions = resolve_request({"skills": ["read_tool_result"]}, ToolAvailability())
    availability = ToolAvailability.from_definitions(definitions, source="requested")
    assert availability.names == {"read_tool_result"}
    _turn("uncertain-result")
    receipt_id = _execution("uncertain-result", status="failed", result=SkillResult(
        success=False, output="Write outcome is unknown.",
        execution_started=True, state_change_unknown=True, retryable=False,
    ))
    denied = await registry.dispatch(
        "read_tool_result", {"receipt_id": receipt_id}, user_id=1,
    )
    assert not denied.success
    read = await registry.dispatch(
        "read_tool_result", {"receipt_id": receipt_id}, user_id=1, actor="main",
        bound_skill=availability.binding_for("read_tool_result"),
    )
    assert read.success and not read.state_changed
    page = json.loads(read.output)
    assert not page["execution"]["ok"] and page["execution"]["started"]
    assert "changed" not in page["execution"]
    assert len(db.get_tool_executions_for_turn("uncertain-result")) == 1


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


def test_exact_tool_requests_load_only_requested_tools_and_report_existing_ones():
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    initial = ToolAvailability()
    result, additions = resolve_request(
        {"skills": ["browse_workspace", "edit_workspace", "browse_workspace"]},
        initial,
    )
    loaded = initial.with_definitions(additions, source="requested")
    assert initial.names == frozenset()
    assert loaded.names == {"browse_workspace", "edit_workspace"}
    assert len(result["loaded"]) == 1
    assert set(result["loaded"][0]["tools"]) == loaded.names
    assert "capability_context" in result["loaded"][0]

    repeated, additions = resolve_request({"skills": ["browse_workspace"]}, loaded)
    assert additions == []
    assert repeated == {
        "ok": True,
        "already_loaded": [
            {"skill": "personal_workspace", "tools": ["browse_workspace"]},
        ],
    }

    mixed, additions = resolve_request(
        {"skills": ["browse_workspace", "run_extension"]}, loaded,
    )
    assert ToolAvailability.from_definitions(additions, source="requested").names == {"run_extension"}
    assert mixed["already_loaded"] == repeated["already_loaded"]
    assert len(mixed["loaded"]) == 1
    assert "capability_context" not in mixed["loaded"][0]


@pytest.mark.parametrize("arguments", [
    {"skills": ["browse_workspace", "personal_workspace"]},
    {"skills": ["personal_workspace", "browse_workspace"]},
    {"skills": ["browse_workspace"], "query": "personal_workspace"},
])
def test_skill_and_query_requests_still_load_the_whole_group(arguments):
    from mochi.request_tools import build_catalog, resolve_request
    from mochi.tool_availability import ToolAvailability

    expected = set(build_catalog().eligible["personal_workspace"].tool_names)
    result, additions = resolve_request(arguments, ToolAvailability())

    assert len(additions) == len(expected)
    assert ToolAvailability.from_definitions(additions, source="requested").names == expected
    assert len(result["loaded"]) == 1
    assert set(result["loaded"][0]["tools"]) == expected
    assert "capability_context" in result["loaded"][0]
    assert not result.get("already_loaded")
    assert not result.get("no_match")


def test_sparse_tool_receipts_preserve_loaded_missing_and_invalid_outcomes():
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    loaded, definitions = resolve_request(
        {"skills": ["view_core_memory"]}, ToolAvailability(),
    )
    assert loaded == {
        "ok": True, "loaded": [{"skill": "memory", "tools": ["view_core_memory"]}],
    }
    assert ToolAvailability.from_definitions(definitions, source="requested").names == {"view_core_memory"}
    missing, definitions = resolve_request(
        {"query": "no_such_capability_xyz"}, ToolAvailability(),
    )
    assert missing == {"ok": True, "no_match": True}
    assert definitions == []
    invalid, definitions = resolve_request({"skills": []}, ToolAvailability())
    assert definitions == []
    assert not invalid["ok"] and not invalid["started"] and not invalid["changed"]
    assert invalid["code"] == "invalid_request" and invalid["retryable"]
    assert invalid["message"]


def test_exact_tool_requests_keep_policy_denials_without_loading_siblings(monkeypatch):
    from mochi import tool_policy
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    monkeypatch.setattr(tool_policy, "_deny_set", {"edit_workspace"})
    result, additions = resolve_request(
        {"skills": ["edit_workspace", "browse_workspace"]}, ToolAvailability(),
    )
    assert ToolAvailability.from_definitions(additions, source="requested").names == {"browse_workspace"}
    assert result["unavailable"] == [
        {"request": "edit_workspace", "reason": "policy_denied"},
    ]


def test_resident_tool_and_skill_requests_do_not_duplicate_existing_tools():
    from mochi import skills
    from mochi.request_tools import resolve_request
    from mochi.tool_availability import ToolAvailability

    availability = ToolAvailability.from_definitions(
        skills.get_tools_by_tool_names(["manage_settings"]), source="resident",
    )
    result, additions = resolve_request(
        {"skills": ["skill_management", "manage_settings", "skill_management"]},
        availability,
    )
    assert additions == []
    assert result["already_loaded"] == [
        {"skill": "skill_management", "tools": ["manage_settings"]},
    ]


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
