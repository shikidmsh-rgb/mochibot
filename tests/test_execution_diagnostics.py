from mochi import runtime_trace as trace
from mochi.db import finish_tool_execution, start_tool_execution
from mochi.execution_diagnostics import summarize, terminal_facts
from mochi.skills.base import SkillResult


STAMP = "2026-10-01T10:00:00+00:00"


def root(status="delivered"):
    return {"span_id": "run", "status": status}


def model(index, status="completed", *, operation="first", outcome="completed", **facts):
    return {
        "id": index, "span_id": f"model-{index}", "span_kind": "model",
        "status": status, "started_at": STAMP, "finished_at": STAMP,
        "facts": {"kind": "model", "operation_id": operation, "outcome": outcome, **facts},
    }


def test_model_failure_recovers_only_within_same_request_not_on_delivery():
    calls = [model(1, "failed"), model(2, operation="different")]
    summary = summarize(root(), calls, [])
    assert summary["state"] == "unresolved"
    assert summary["delivery"] == "confirmed"
    calls.append(model(3))
    summary = summarize(root(), calls, [])
    assert summary["state"] == "recovered"
    assert summary["issues"][0]["recovered_by"] == "span:model-3"
    assert summary["models"] == {"failed": 1, "completed": 2}


def test_late_response_cannot_recover_cancelled_request():
    summary = summarize(root("cancelled"), [
        model(1, "failed"), model(2, "completed_late"),
    ], [])
    assert summary["counts"]["recovered"] == 0
    assert summary["models"]["late"] == 1
    assert any(item["code"] == "late_response" and item["state"] == "unknown"
               for item in summary["issues"])
    cancelled = summarize(root("cancelled"), [model(1, "cancelled")], [])
    assert cancelled["state"] == "unknown"
    assert cancelled["models"] == {"cancelled": 1}


def test_confirmed_delivery_recovers_rejection_but_not_unknown_prior_send():
    def delivery(index, outcome, confirmed):
        return {
            "id": index, "span_id": f"send-{index}", "span_kind": "delivery",
            "status": "completed" if confirmed else "failed",
            "started_at": STAMP, "finished_at": STAMP,
            "facts": {
                "kind": "delivery_attempt", "operation_id": "whole_reply",
                "outcome": outcome, "confirmed": confirmed,
            },
        }
    recovered = summarize(root(), [
        delivery(1, "delivery_rejected", False), delivery(2, "delivered", True),
    ], [])
    assert recovered["state"] == "recovered"
    assert recovered["delivery"] == "confirmed"
    assert recovered["issues"][0]["recovered_by"] == "span:send-2"
    uncertain = summarize(root(), [
        delivery(1, "delivery_unknown", False), delivery(2, "delivered", True),
    ], [])
    assert uncertain["state"] == "unknown"
    assert uncertain["delivery"] == "confirmed"
    assert uncertain["counts"]["recovered"] == 0


def test_output_limit_is_not_truncation_and_requires_known_accounting():
    exact = terminal_facts(
        reason="length", used=128, limit=128, verified_limit=True, operation_id="one",
    )
    excess = terminal_facts(
        reason="stop", used=129, limit=128, verified_limit=True, operation_id="one",
    )
    unknown = terminal_facts(
        reason="stop", used=129, limit=128, verified_limit=False, operation_id="one",
    )
    assert exact["outcome"] == "truncated" and exact["over_limit"] is False
    assert excess["outcome"] == "completed" and excess["over_limit"] is True
    assert unknown["over_limit"] is None and not unknown["limit_checked"]
    summary = summarize(root(), [
        model(1, **{k: v for k, v in excess.items() if k not in {"kind", "operation_id"}}),
        model(2),
    ], [])
    assert summary["state"] == "unresolved"
    assert summary["issues"][0]["code"] == "output_budget_exceeded"
    assert summary["issues"][0]["requested"] == 128
    assert summary["issues"][0]["actual"] == 129


def test_silence_is_not_failure_but_missing_terminal_evidence_is_unknown():
    assert summarize(root("skip"), [], [])["state"] == "no_detected_error"
    old_call = model(1)
    old_call["facts"] = None
    summary = summarize(root("skip"), [old_call], [])
    assert summary["state"] == "unknown"
    assert summary["delivery"] == "not_requested"


def tool_call(run, name, args, result, *, target=None):
    execution_id = start_tool_execution(
        turn_id=run.turn_id, tool_call_id=f"call-{name}", user_id=0, source="runtime:free_time",
        skill_name="workspace", tool_name=name, action="update", arguments_json="{}",
    )
    item = trace.start_tool(execution_id, name, args, document_target=target)
    finish_tool_execution(
        execution_id, status="success" if result.success else "failed",
        state_changed=result.state_changed if result.success else False,
        result_summary=result.output,
    )
    trace.finish_tool(item, result)
    return execution_id


def test_document_retry_preserves_failure_and_links_recovery_to_same_target():
    with trace.run_scope("documents", "free_time", 0, {}) as run:
        first = tool_call(run, "write_diary", {"content": "old"}, SkillResult(success=False),
                          target="2026-10-01")
        tool_call(run, "write_diary", {"content": "other day"}, SkillResult(state_changed=True),
                  target="2026-10-02")
        trace.finish_run(run.trace_id, "delivered")
        summary = trace.get_run(0, run.trace_id)["diagnostics"]
        assert summary["state"] == "unresolved"
        recovered = tool_call(
            run, "write_diary", {"content": "corrected"}, SkillResult(state_changed=True),
            target="2026-10-01",
        )
        summary = trace.get_run(0, run.trace_id)["diagnostics"]
    assert summary["state"] == "recovered"
    assert summary["issues"][0]["ref"] == f"tool:{first}"
    assert summary["issues"][0]["recovered_by"] == f"tool:{recovered}"
    assert summary["tools"] == {"failed": 1, "success": 2}
    assert summary["data_changes"] == {"no_change": 1, "confirmed": 2}
    page = trace.list_runs(0)["runs"][0]["diagnostics"]
    assert page == {key: value for key, value in summary.items() if key != "issues"}


def test_redacted_arguments_and_unknown_effects_never_claim_recovery():
    with trace.run_scope("uncertain", "chat", 0, {}) as run:
        tool_call(run, "manage_settings", {"action": "set", "value": "secret-one"},
                  SkillResult(success=False))
        tool_call(run, "manage_settings", {"action": "set", "value": "secret-two"},
                  SkillResult(state_changed=True))
        tool_call(run, "write_diary", {"content": "maybe written"},
                  SkillResult(success=False, state_change_unknown=True), target="2026-10-01")
        tool_call(run, "write_diary", {"content": "written"},
                  SkillResult(state_changed=True), target="2026-10-01")
        trace.finish_run(run.trace_id, "delivered")
    summary = trace.get_run(0, run.trace_id)["diagnostics"]
    assert summary["counts"]["recovered"] == 0
    assert summary["counts"]["unresolved"] == 2
    assert summary["counts"]["unknown"] == 1
    assert summary["delivery"] == "confirmed"


def test_failed_operation_can_still_report_a_confirmed_partial_change():
    with trace.run_scope("partial-write", "chat", 0, {}) as run:
        tool_call(run, "partial_writer", {"id": 1},
                  SkillResult(success=False, state_changed=True))
        trace.finish_run(run.trace_id, "delivered")
    summary = trace.get_run(0, run.trace_id)["diagnostics"]
    assert summary["tools"] == {"failed": 1}
    assert summary["data_changes"] == {"confirmed": 1}
    assert summary["state"] == "unresolved"


def test_same_arguments_recover_but_separate_runs_do_not_share_tool_success():
    with trace.run_scope("shared-turn", "chat", 0, {}) as first:
        tool_call(first, "manage_todo", {"id": 1}, SkillResult(success=False))
        trace.finish_run(first.trace_id, "failed")
    with trace.run_scope("shared-turn", "chat", 0, {}) as second:
        tool_call(second, "manage_todo", {"id": 1}, SkillResult(success=False))
        tool_call(second, "manage_todo", {"id": 1}, SkillResult())
        trace.finish_run(second.trace_id, "delivered")
    original = trace.get_run(0, first.trace_id)["diagnostics"]
    recovered = trace.get_run(0, second.trace_id)["diagnostics"]
    assert original["counts"]["recovered"] == 0
    assert original["tools"] == {"failed": 1}
    assert recovered["state"] == "recovered"


def test_rejected_call_and_unfinished_tool_are_visible_without_inventing_changes():
    with trace.run_scope("unfinished", "chat", 0, {}) as run:
        trace.event("tool_results", facts={
            "kind": "rejections",
            "items": [{"call_id": "bad", "tool": "write_diary", "code": "incomplete_tool_call"}],
        })
        execution_id = start_tool_execution(
            turn_id=run.turn_id, tool_call_id="unfinished", user_id=0, source="chat",
            skill_name="workspace", tool_name="write_diary", action="update", arguments_json="{}",
        )
        trace.start_tool(execution_id, "write_diary", {}, document_target="2026-10-01")
        trace.finish_run(run.trace_id, "interrupted")
    summary = trace.get_run(0, run.trace_id)["diagnostics"]
    codes = {item["code"] for item in summary["issues"]}
    assert {"tool_rejected", "tool_result_missing", "side_effects_unknown"} <= codes
    assert summary["data_changes"] == {"unknown": 1}
    assert summary["tools"]["rejected"] == 1
