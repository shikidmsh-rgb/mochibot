"""Deterministic, human-only projections of recorded execution outcomes."""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit


def _field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def model_facts(
    *, protocol: str, endpoint: str, request: dict, response: Any, operation_id: str,
) -> dict:
    """Interpret only terminal fields and usage from the selected protocol."""
    reason = None
    used = limit = None
    usage = _field(response, "usage")
    host = urlsplit(endpoint).hostname or ""
    official_openai = (
        host == "api.openai.com" or host.endswith(".openai.azure.com")
        or host.endswith(".services.ai.azure.com")
    )
    verified_limit = False
    if protocol == "chat.completions.create":
        choices = _field(response, "choices", []) or []
        reason = _field(choices[0], "finish_reason") if choices else None
        used = _count(_field(usage, "completion_tokens"))
        key = "max_completion_tokens" if "max_completion_tokens" in request else "max_tokens"
        limit = _count(request.get(key))
        verified_limit = official_openai or host == "api.deepseek.com"
    elif protocol == "responses.create":
        reason = _field(response, "status")
        used = _count(_field(usage, "output_tokens"))
        limit = _count(request.get("max_output_tokens"))
        verified_limit = official_openai
        if reason == "incomplete" and _field(
            _field(response, "incomplete_details"), "reason",
        ) == "max_output_tokens":
            reason = "max_output_tokens"
    elif protocol == "messages.create":
        reason = _field(response, "stop_reason")
        used = _count(_field(usage, "output_tokens"))
        limit = _count(request.get("max_tokens"))
        verified_limit = host == "api.anthropic.com"
    elif protocol == "embeddings.create":
        reason = "completed" if _field(response, "data") is not None else None

    return terminal_facts(
        reason=reason, used=used, limit=limit,
        verified_limit=verified_limit, operation_id=operation_id,
    )


def terminal_facts(
    *, reason: str | None, used: int | None, limit: int | None,
    verified_limit: bool, operation_id: str,
) -> dict:
    if reason in {"stop", "tool_calls", "completed", "end_turn", "tool_use", "stop_sequence"}:
        outcome = "completed"
    elif reason in {"length", "max_tokens", "max_output_tokens"}:
        outcome = "truncated"
    elif reason in {"error", "failed", "cancelled", "incomplete", "content_filter", "refusal"}:
        outcome = "failed"
    else:
        outcome = "unknown"
    return {
        "kind": "model", "operation_id": operation_id, "outcome": outcome,
        "terminal_reason": reason, "output_tokens": used, "output_limit": limit,
        "limit_checked": verified_limit and used is not None and limit is not None,
        "over_limit": (
            used > limit if verified_limit and used is not None and limit is not None else None
        ),
    }


def summarize(root: dict, spans: list[dict], tools: list[dict]) -> dict:
    """Never equate a delivered reply or later unrelated success with recovery."""
    issues: list[dict] = []
    models: Counter = Counter()
    tool_counts: Counter = Counter()
    changes: Counter = Counter()
    pending_models: dict[str, list[dict]] = {}
    pending_tools: dict[str, list[dict]] = {}
    pending_deliveries: dict[str, list[dict]] = {}
    root_active = root["status"] in {"running", "prepared"}

    def issue(source: str, ref: str, code: str, state="unresolved", **details) -> dict:
        item = {"source": source, "ref": ref, "code": code, "state": state, **details}
        issues.append(item)
        return item

    def recover(pending: dict, key: str | None, ref: str) -> None:
        if key:
            for item in pending.pop(key, []):
                item.update(state="recovered", recovered_by=ref)

    def remember(pending: dict, key: str | None, item: dict) -> None:
        if key:
            pending.setdefault(key, []).append(item)

    facts_by_execution = {
        row["facts"]["execution_id"]: row["facts"]
        for row in spans
        if row.get("facts") and row["facts"].get("kind") == "tool"
    }
    events = []
    for row in spans:
        if row["span_kind"] == "model" or (row.get("facts") or {}).get("kind") in {
            "rejections", "delivery_attempt",
        }:
            events.append((row.get("finished_at") or row["started_at"], row["id"], "span", row))
    for row in tools:
        events.append((row.get("finished_at") or row["started_at"], row["id"], "tool", row))
    # ISO offsets can differ across runtime and ledger records.
    from datetime import datetime
    events.sort(key=lambda event: (datetime.fromisoformat(event[0]).timestamp(), event[1]))

    for _, _, kind, row in events:
        if kind == "tool":
            ref = f"tool:{row['id']}"
            facts = facts_by_execution.get(row["id"], {})
            status = row["status"]
            unknown_change = facts.get("state_change_unknown", not bool(row["state_changed"]))
            if facts.get("state_changed", bool(row["state_changed"])):
                changes["confirmed"] += 1
            elif unknown_change:
                changes["unknown"] += 1
            else:
                changes["no_change"] += 1
            tool_counts[status] += 1
            key = facts.get("operation_id")
            if status == "success":
                recover(pending_tools, key, ref)
            elif status == "failed":
                item = issue(
                    "tool", ref, "tool_failed", tool=row["tool_name"],
                    error_code=facts.get("error_code") or None,
                )
                if not unknown_change:
                    remember(pending_tools, key, item)
            else:
                issue("tool", ref, "tool_result_missing", "pending" if root_active else "unknown",
                      tool=row["tool_name"])
            if unknown_change:
                issue("data", ref, "side_effects_unknown", "unknown", tool=row["tool_name"])
            continue

        facts = row.get("facts") or {}
        ref = f"span:{row['span_id']}"
        if facts.get("kind") == "delivery_attempt":
            key = facts.get("operation_id")
            if facts.get("confirmed") is True:
                recover(pending_deliveries, key, ref)
            else:
                code = facts.get("outcome") or "delivery_unknown"
                item = issue(
                    "delivery", ref, code,
                    "pending" if row["status"] == "running" and root_active
                    else "unknown" if code == "delivery_unknown" else "unresolved",
                )
                # A retry cannot disprove an earlier unconfirmed send.
                if code != "delivery_unknown":
                    remember(pending_deliveries, key, item)
            continue
        if facts.get("kind") == "rejections":
            for rejected in facts.get("items", []):
                tool_counts["rejected"] += 1
                issue(
                    "tool", ref, "tool_rejected", tool=rejected.get("tool"),
                    call_id=rejected.get("call_id"), error_code=rejected.get("code"),
                )
            continue
        status = row["status"]
        outcome = facts.get("outcome", "unknown")
        key = facts.get("operation_id")
        if status == "completed_late":
            models["late"] += 1
            issue("model", ref, "late_response", "unknown")
        elif status == "failed":
            models["failed"] += 1
            item = issue("model", ref, "request_failed")
            remember(pending_models, key, item)
        elif status in {"cancelled", "interrupted"}:
            models[status] += 1
            issue("model", ref, "request_interrupted", "unknown")
        elif status == "running":
            models["pending"] += 1
            issue("model", ref, "request_unfinished", "pending" if root_active else "unknown")
        elif outcome == "completed":
            models["completed"] += 1
            recover(pending_models, key, ref)
        elif outcome in {"failed", "truncated"}:
            models[outcome] += 1
            item = issue("model", ref, "output_truncated" if outcome == "truncated" else "response_failed",
                         terminal_reason=facts.get("terminal_reason"))
            remember(pending_models, key, item)
        else:
            models["unknown"] += 1
            issue("model", ref, "terminal_evidence_missing", "unknown")
        if facts.get("over_limit") is True:
            # An already-observed budget breach is not undone by a later small response.
            issue("protocol", ref, "output_budget_exceeded",
                  requested=facts["output_limit"], actual=facts["output_tokens"])

    delivery = (
        "confirmed" if root["status"] == "delivered"
        else "pending" if root["status"] == "prepared"
        else "not_requested" if root["status"] in {"skip", "handled", "tools_only", "completed"}
        else "unknown"
    )
    if root["status"] in {"failed", "invalid", "cancelled", "interrupted", "retry_scheduled"}:
        issue("runtime", f"span:{root['span_id']}", "run_not_completed",
              "unknown" if root["status"] in {"cancelled", "interrupted"} else "unresolved")
    if root["status"] in {"delivery_unknown", "delivery_rejected", "delivery_unavailable"}:
        issue("delivery", f"span:{root['span_id']}", root["status"],
              "unknown" if root["status"] == "delivery_unknown" else "unresolved")
    counts = Counter(item["state"] for item in issues)
    state = (
        "unresolved" if counts["unresolved"] else "unknown" if counts["unknown"]
        else "pending" if root_active or counts["pending"]
        else "recovered" if counts["recovered"] else "no_detected_error"
    )
    return {
        "state": state, "issues": issues,
        "counts": {name: counts[name] for name in ("unresolved", "unknown", "pending", "recovered")},
        "models": dict(models), "tools": dict(tool_counts),
        "data_changes": dict(changes), "delivery": delivery,
    }
