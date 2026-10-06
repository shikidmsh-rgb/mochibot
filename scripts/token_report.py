#!/usr/bin/env python3
"""Read a complete time window of recorded token and cache evidence without model calls."""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mochi import runtime_trace
from mochi.token_accounting import usage_units
from scripts.diagnose import timestamp

METRICS = ("input_total", "input", "output", "cache_read", "cache_write", "reasoning")
EVALUATION_KINDS = frozenset({"evaluation", "cache_evaluation"})


def summarize(calls: list[dict]) -> dict:
    metrics = {
        metric: {
            "known_tokens": sum(call["units"].get(metric) or 0 for call in calls),
            "unknown_calls": sum(call["units"].get(metric) is None for call in calls),
        }
        for metric in METRICS
    }
    covered = [
        call for call in calls
        if call["units"].get("input_total") is not None
        and call["units"].get("cache_read") is not None
    ]
    total = sum(call["units"]["input_total"] for call in covered)
    cached = sum(call["units"]["cache_read"] for call in covered)
    return {
        "attempts": len(calls), "metrics": metrics,
        "cache_read_fraction": cached / total if total else None,
        "cache_ratio_covered_calls": len(covered),
        "anomalous_attempts": sum(call["anomalous"] for call in calls),
        "additional_attempts": sum(
            call["attempt_index"] > 1 or call["stage"] == "compression_retry" for call in calls
        ),
    }


def grouped(calls: list[dict], key: str) -> list[dict]:
    groups = defaultdict(list)
    for call in calls:
        groups[str(call[key])].append(call)
    return [{"name": name, **summarize(items)} for name, items in sorted(groups.items())]


def compare_cache(previous: dict, current: dict) -> dict | None:
    left, right = previous["fingerprint"], current["fingerprint"]
    required = ("tools", "tool_order", "system", "input_prefixes")
    if not all(key in left and key in right for key in required):
        return None
    shared = 0
    for a, b in zip(left["input_prefixes"], right["input_prefixes"]):
        if a != b:
            break
        shared += 1
    return {
        "previous_span_id": previous["span_id"],
        "gap_seconds": (
            datetime.fromisoformat(current["started_at"])
            - datetime.fromisoformat(previous["started_at"])
        ).total_seconds(),
        "same_tools": left["tools"] == right["tools"],
        "same_tool_order": left["tool_order"] == right["tool_order"],
        "same_system": left["system"] == right["system"],
        "shared_input_items": shared,
        "previous_input_items": len(left["input_prefixes"]),
        "same_cache_policy": left.get("cache_policy") == right.get("cache_policy"),
        "same_reasoning_setting": left.get("reasoning") == right.get("reasoning"),
        "previous_cached_tokens": previous["units"].get("cache_read"),
        "current_cached_tokens": current["units"].get("cache_read"),
        "provider_cache_cause": "not_determined",
    }


def build_report(
    conn, *, user_id: int, since: str, until: str,
    kind: str | None = None, model: str | None = None, role: str | None = None,
    stage: str | None = None, exclude_evaluations: bool = False,
    include_calls: bool = False,
) -> dict:
    start, end = datetime.fromisoformat(since), datetime.fromisoformat(until)
    if start.utcoffset() is None or end.utcoffset() is None or start >= end:
        raise ValueError("Use an increasing timezone-aware interval")
    conn.execute("BEGIN")
    rows = conn.execute(
        "SELECT r.id,r.trace_id,r.span_id,r.turn_id,r.run_kind,r.name,r.status,"
        "r.started_at,r.finished_at,r.duration_ms,r.facts_json,"
        "json_extract(r.request_json,'$.provider') AS provider,"
        "json_extract(r.request_json,'$.endpoint') AS endpoint,"
        "json_extract(r.request_json,'$.model') AS model,"
        "u.id AS usage_id,u.model_role,u.purpose,u.usage_stage,u.call_type,"
        "root.status AS run_status "
        "FROM runtime_traces r LEFT JOIN usage_log u ON u.model_span_id=r.span_id "
        "LEFT JOIN runtime_traces root ON root.span_id=r.trace_id "
        "WHERE r.span_kind='model' AND r.user_id=? "
        "AND julianday(r.started_at)>=julianday(?) "
        "AND julianday(r.started_at)<julianday(?) "
        "ORDER BY julianday(r.started_at),r.id",
        (user_id, since, until),
    )
    calls = []
    sources = defaultdict(lambda: {"reference_tokens": 0, "chars": 0, "uncounted_parts": 0})
    attempts = defaultdict(int)
    last_cohort = {}
    for row in rows:
        facts = json.loads(row["facts_json"] or "{}")
        distribution = facts.get("token_distribution") or {}
        billing = facts.get("billing") or {}
        protocol = billing.get("protocol") or row["name"].rsplit(":", 1)[-1]
        call_role = facts.get("model_role") or row["model_role"] or "unknown"
        if call_role == "P":
            call_role = "unknown"
        activity = facts.get("purpose") or row["purpose"] or row["run_kind"]
        call_stage = facts.get("usage_stage") or row["usage_stage"] or "unknown"
        evaluation = row["run_kind"] in EVALUATION_KINDS or row["call_type"] == "evaluation"
        if (
            (kind and row["run_kind"] != kind)
            or (model and row["model"] != model)
            or (role and call_role != role)
            or (stage and call_stage != stage)
            or (exclude_evaluations and evaluation)
        ):
            continue
        units = billing.get("units")
        if units is None:
            units = usage_units(distribution.get("provider_usage"), protocol)
        operation = (row["trace_id"], facts.get("operation_id") or row["span_id"])
        attempts[operation] += 1
        terminal = facts.get("outcome", "unknown")
        call = {
            "span_id": row["span_id"], "trace_id": row["trace_id"], "turn_id": row["turn_id"],
            "usage_id": row["usage_id"], "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "provider": billing.get("provider") or row["provider"] or "unknown",
            "endpoint": billing.get("endpoint") or row["endpoint"] or "unknown",
            "model": row["model"] or "unknown", "role": call_role,
            "activity": activity, "stage": call_stage,
            "evaluation": evaluation, "status": row["status"],
            "run_status": row["run_status"], "terminal": terminal,
            "duration_ms": row["duration_ms"], "units": units,
            "provider_usage": distribution.get("provider_usage"),
            "attempt_index": attempts[operation],
            "anomalous": row["status"] != "completed" or terminal in {"failed", "truncated"},
            "fingerprint": facts.get("cache_fingerprint") or {},
        }
        # Different roles/stages and evaluation traffic are never cache controls for each other.
        cohort = (
            call["provider"], call["endpoint"], call["model"], call_role,
            activity, call_stage, evaluation,
        )
        previous = last_cohort.get(cohort)
        comparable = row["status"] == "completed" and terminal == "completed"
        call["cache_comparison"] = (
            compare_cache(previous, call)
            if previous and comparable and previous["finished_at"]
            and datetime.fromisoformat(previous["finished_at"]) <= datetime.fromisoformat(call["started_at"])
            and call_role != "unknown" and call_stage != "unknown" else None
        )
        if comparable:
            last_cohort[cohort] = call
        calls.append(call)
        for part in distribution.get("parts", []):
            source = sources[(distribution.get("encoding", "unknown"), part["source"])]
            source["chars"] += part["chars"]
            source["reference_tokens"] += part["reference_tokens"] or 0
            source["uncounted_parts"] += part["reference_tokens"] is None
    # These rows cannot be matched safely by timestamp/model. Never add them to attempt totals.
    unlinked = conn.execute(
        "SELECT count(*) FROM usage_log u WHERE julianday(u.created_at)>=julianday(?) "
        "AND julianday(u.created_at)<julianday(?) "
        "AND (u.model_span_id IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM runtime_traces r WHERE r.span_id=u.model_span_id))",
        (since, until),
    ).fetchone()[0]
    oldest = conn.execute(
        "SELECT started_at FROM runtime_traces WHERE user_id=? AND span_kind='model' "
        "ORDER BY julianday(started_at),id LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.rollback()
    comparison_groups = defaultdict(list)
    for call in calls:
        comparison = call["cache_comparison"]
        if comparison is not None:
            category = (
                "tools_changed" if not comparison["same_tools"]
                else "system_changed" if not comparison["same_system"]
                else "cache_policy_changed" if not comparison["same_cache_policy"]
                else "reasoning_setting_changed" if not comparison["same_reasoning_setting"]
                else "same_tools_and_system"
            )
            cohort = tuple(call[key] for key in (
                "provider", "endpoint", "model", "role", "activity", "stage", "evaluation",
            ))
            comparison_groups[(cohort, category)].append(call)
    result = {
        "scope": {
            "user_id": user_id, "since": since, "until_exclusive": until,
            "kind": kind, "model": model, "role": role, "stage": stage,
            "exclude_evaluations": exclude_evaluations,
        },
        "coverage": {
            "retention_days": runtime_trace.RETENTION_DAYS,
            "starts_before_retention": start < datetime.now(timezone.utc) - timedelta(days=runtime_trace.RETENTION_DAYS),
            "oldest_retained_attempt": oldest[0] if oldest else None,
            "linked_usage_rows": sum(call["usage_id"] is not None for call in calls),
            "unlinked_instance_usage_rows_in_window": unlinked,
            "unlinked_usage_included_in_totals": False,
            "fingerprinted_calls": sum(bool(call["fingerprint"].get("tools")) for call in calls),
        },
        "summary": summarize(calls),
        "production_or_unlabelled": summarize([call for call in calls if not call["evaluation"]]),
        "evaluations": summarize([call for call in calls if call["evaluation"]]),
        "anomalies": summarize([call for call in calls if call["anomalous"]]),
        "by_activity": grouped(calls, "activity"), "by_model": grouped(calls, "model"),
        "by_role": grouped(calls, "role"), "by_stage": grouped(calls, "stage"),
        "turns": grouped(calls, "turn_id"),
        "input_sources": [
            {"encoding": encoding, "source": source, **counts}
            for (encoding, source), counts in sorted(sources.items())
        ],
        "cache_cohorts": [
            {
                "name": name,
                "cohort": dict(zip(
                    ("provider", "endpoint", "model", "role", "activity", "stage", "evaluation"),
                    cohort,
                )),
                **summarize(items),
            }
            for (cohort, name), items in sorted(comparison_groups.items())
        ],
        "notes": [
            "Token totals come from recorded SDK attempts; missing provider usage stays unknown.",
            "Local reference tokens are not provider token counts. Reasoning is part of output, not added again.",
            "Cache comparisons are client-visible observations, not proof of cause or provider warmup.",
            "Unknown usage and unlinked historical rows are not zero. Detailed evidence lasts seven days.",
        ],
    }
    reference_totals = defaultdict(int)
    for item in result["input_sources"]:
        reference_totals[item["encoding"]] += item["reference_tokens"]
    for item in result["input_sources"]:
        total = reference_totals[item["encoding"]]
        item["share_of_counted_reference_tokens"] = item["reference_tokens"] / total if total else None
    if include_calls:
        result["calls"] = [
            {key: value for key, value in call.items() if key != "fingerprint"} for call in calls
        ]
    statuses = defaultdict(set)
    for call in calls:
        statuses[call["turn_id"]].add(call["run_status"] or "unknown")
    for turn in result["turns"]:
        turn["recorded_run_statuses"] = sorted(statuses[turn["name"]])
    return result


def render(report: dict) -> str:
    def metric(group: dict, name: str) -> str:
        counts = group["metrics"][name]
        text = str(counts["known_tokens"])
        if counts["unknown_calls"]:
            text += f" + unknown ({counts['unknown_calls']} calls)"
        return text

    total = report["summary"]
    lines = [
        f"Window: {report['scope']['since']} .. {report['scope']['until_exclusive']} (end exclusive)",
        f"SDK attempts: {total['attempts']}; additional attempts: {total['additional_attempts']}; "
        f"anomalous: {total['anomalous_attempts']}",
        f"Input tokens: {metric(total, 'input_total')}; output tokens: {metric(total, 'output')}",
        f"Cache read tokens: {metric(total, 'cache_read')}; "
        f"cache write tokens: {metric(total, 'cache_write')}",
        f"Evaluation attempts: {report['evaluations']['attempts']}; "
        f"unlinked historical instance ledger rows (NOT added): "
        f"{report['coverage']['unlinked_instance_usage_rows_in_window']}",
        "",
        "Activity | requests | input tokens | output tokens | cache read | cache write",
    ]
    for group in report["by_activity"]:
        lines.append(
            f"{group['name']} | {group['attempts']} | "
            f"{metric(group, 'input_total')} | {metric(group, 'output')} | "
            f"{metric(group, 'cache_read')} | {metric(group, 'cache_write')}"
        )
    lines.extend(["", "Model | requests | input tokens | output tokens | cache read | cache write"])
    for group in report["by_model"]:
        lines.append(
            f"{group['name']} | {group['attempts']} | "
            f"{metric(group, 'input_total')} | {metric(group, 'output')} | "
            f"{metric(group, 'cache_read')} | {metric(group, 'cache_write')}"
        )
    lines.extend(["", "Largest input sources (local reference tokens)"])
    for item in sorted(report["input_sources"], key=lambda item: item["reference_tokens"], reverse=True)[:10]:
        share = item["share_of_counted_reference_tokens"]
        label = f"{share:.1%}" if share is not None else "unknown"
        lines.append(
            f"{item['source']} [{item['encoding']}]: {item['reference_tokens']} ({label}); "
            f"uncounted parts: {item['uncounted_parts']}"
        )
    lines.extend(["", "Cache cohort | requests | weighted read fraction | covered requests"])
    for group in report["cache_cohorts"]:
        fraction = group["cache_read_fraction"]
        label = f"{fraction:.1%}" if fraction is not None else "unknown"
        cohort = group["cohort"]
        lines.append(
            f"{group['name']} [{cohort['model']}/{cohort['role']}/{cohort['activity']}/{cohort['stage']}] "
            f"| {group['attempts']} | {label} | {group['cache_ratio_covered_calls']}"
        )
    lines.extend(["", *report["notes"]])
    if report["coverage"]["starts_before_retention"]:
        lines.append("PARTIAL COVERAGE: requested start is older than detailed evidence retention.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--days", type=float)
    window.add_argument("--since", type=timestamp)
    parser.add_argument("--until", type=timestamp)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--kind")
    parser.add_argument("--model")
    parser.add_argument("--role")
    parser.add_argument("--stage")
    parser.add_argument("--exclude-evaluations", action="store_true")
    parser.add_argument("--calls", action="store_true", help="Include per-request metadata in JSON, never prompt bodies.")
    parser.add_argument("--json", action="store_true", help="Emit the full structured report.")
    args = parser.parse_args()
    if args.calls and not args.json:
        parser.error("--calls requires --json")
    from mochi.config import OWNER_USER_ID

    end = datetime.fromisoformat(args.until) if args.until else datetime.now(timezone.utc)
    days = args.days if args.days is not None else 1
    if not 0 < days <= 366:
        parser.error("--days must be positive and at most 366")
    since = args.since or (end - timedelta(days=days)).isoformat()
    try:
        with closing(runtime_trace.read_connection()) as conn:
            report = build_report(
                conn, user_id=args.user_id if args.user_id is not None else OWNER_USER_ID or 0,
                since=since, until=end.isoformat(), kind=args.kind, model=args.model,
                role=args.role, stage=args.stage, exclude_evaluations=args.exclude_evaluations,
                include_calls=args.calls,
            )
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))


if __name__ == "__main__":
    main()
