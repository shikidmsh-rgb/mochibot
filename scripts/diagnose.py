#!/usr/bin/env python3
"""Read recorded runtime evidence without invoking Mochi or a model.

Examples:
  python scripts/diagnose.py --kind free_time --state unresolved --state unknown
  python scripts/diagnose.py --since 2026-10-01T20:00:00+08:00 --until 2026-10-01T23:00:00+08:00
  python scripts/diagnose.py --trace TRACE_ID --export incident.json
  python scripts/diagnose.py --trace TRACE_ID --journal-unit mochibot.service --export incident.json

The default window is the last 24 hours. An export includes existing trace
payloads, not a reconstruction. It may contain private conversation content.
No models, tool handlers, schema initialization, retries or state changes run.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mochi import runtime_trace as trace


STATES = ("unresolved", "unknown", "pending", "recovered", "no_detected_error")


def timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO timestamp with a timezone offset.") from exc
    if parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Timestamp must include a timezone offset.")
    return parsed.astimezone(timezone.utc).isoformat()


def bounded_limit(value: str) -> int:
    count = int(value)
    if not 1 <= count <= 100:
        raise argparse.ArgumentTypeError("Limit must be between 1 and 100.")
    return count


def load_export_secrets() -> None:
    """Use existing encrypted credentials only for redaction; never load clients."""
    from mochi.admin.admin_crypto import decrypt_api_key, is_encrypted

    with closing(trace.read_connection()) as conn:
        values = [
            row[0] for row in conn.execute("SELECT api_key FROM model_registry WHERE api_key!=''")
        ]
        values.extend(
            row[0] for row in conn.execute("SELECT value FROM skill_config")
            if is_encrypted(row[0])
        )
    for value in values:
        decoded = decrypt_api_key(value)
        if value and not decoded:
            raise ValueError("Cannot load credential redactions; export was not created.")
        trace.register_secret(decoded)


def read_journal(unit: str, since: str, until: str) -> dict:
    if re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", unit) is None:
        raise ValueError("Journal unit must be an explicit systemd .service name.")
    def journal_time(value: str, *, end: bool = False) -> str:
        instant = datetime.fromisoformat(value).astimezone(timezone.utc)
        if end and instant.microsecond:
            instant += timedelta(seconds=1)
        return instant.strftime("%Y-%m-%d %H:%M:%S UTC")

    try:
        result = subprocess.run(
            [
                "journalctl", "--unit", unit, "--since", journal_time(since),
                "--until", journal_time(until, end=True), "--no-pager", "--output=json",
                "--lines=301",
            ],
            text=True, encoding="utf-8", errors="replace",
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unavailable", "error": type(exc).__name__, "unit": unit}
    if result.returncode:
        return {
            "status": "unavailable", "unit": unit,
            "error": result.stderr.strip() or f"journalctl exit {result.returncode}",
        }
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    events = []
    for line in lines[-300:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return {"status": "unavailable", "unit": unit, "error": "Invalid journal JSON."}
        events.append({
            key: event[key] for key in (
                "__REALTIME_TIMESTAMP", "PRIORITY", "SYSLOG_IDENTIFIER", "_PID", "MESSAGE",
            ) if key in event
        })
    return {
        "status": "partial" if result.stderr.strip() else "available",
        "unit": unit, "since": since, "until": until,
        "events": events, "truncated": len(lines) > 300,
        "notice": result.stderr.strip() or None,
    }


def select_runs(
    user_id: int, *, since: str, until: str, kind: str | None,
    states: list[str], limit: int,
) -> tuple[list[dict], bool, int]:
    found = []
    before = None
    scanned = 0
    while True:
        page = trace.list_runs(
            user_id, limit=30, before=before, since=since, until=until, kind=kind,
        )
        for index, run in enumerate(page["runs"]):
            scanned += 1
            if states and run["diagnostics"]["state"] not in states:
                continue
            found.append(run)
            if len(found) == limit:
                more = index < len(page["runs"]) - 1 or page["next_before"] is not None
                return found, more, scanned
        before = page["next_before"]
        if before is None:
            return found, False, scanned


def build_report(args, *, now: datetime | None = None) -> dict:
    from mochi.config import OWNER_USER_ID

    current = now or datetime.now(timezone.utc)
    since = args.since or (current - timedelta(hours=24)).isoformat()
    until = args.until or current.isoformat()
    if datetime.fromisoformat(since) > datetime.fromisoformat(until):
        raise ValueError("--since must not be after --until.")
    user_id = args.user_id if args.user_id is not None else OWNER_USER_ID
    if user_id is None:
        user_id = 0
    load_export_secrets()
    records = []
    if args.trace:
        detail = trace.get_run(user_id, args.trace)
        if detail is None:
            raise ValueError("Trace not found for this user; it may be outside retention.")
        records.append(detail)
        root = detail["spans"][0]
        runs = [{
            key: root[key] for key in (
                "trace_id", "turn_id", "run_kind", "status", "started_at", "finished_at",
            )
        }]
        runs[0]["diagnostics"] = detail["diagnostics"]
        since = root["started_at"]
        ends = [row["finished_at"] for row in detail["spans"] if row["finished_at"]]
        active = any(row["status"] in {"running", "prepared"} for row in detail["spans"])
        until = (
            max(ends, key=lambda item: datetime.fromisoformat(item))
            if ends and not active else current.isoformat()
        )
        more, scanned = False, 1
    else:
        runs, more, scanned = select_runs(
            user_id, since=since, until=until, kind=args.kind,
            states=args.state or [], limit=args.limit,
        )
        if args.export:
            for run in runs:
                detail = trace.get_run(user_id, run["trace_id"])
                records.append(detail or {
                    "trace_id": run["trace_id"], "unavailable": "Trace expired during export.",
                })
    report = {
        "generated_at": current.isoformat(),
        "current_checkout": trace.code_version(),
        "scope": {"user_id": user_id, "since": since, "until": until,
                  "kind": args.kind, "states": args.state or []},
        "scanned_runs": scanned, "more_candidates": more,
        "retention_days": trace.RETENTION_DAYS,
        "runs": runs,
        "journal": read_journal(args.journal_unit, since, until) if args.journal_unit else {
            "status": "not_requested",
        },
    }
    if records:
        report["records"] = records
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--since", type=timestamp)
    result.add_argument("--until", type=timestamp)
    result.add_argument("--kind", help="Exact runtime kind, e.g. chat, free_time, bedtime.")
    result.add_argument("--state", action="append", choices=STATES, help="Repeat to include several states.")
    result.add_argument("--limit", type=bounded_limit, default=10)
    result.add_argument("--user-id", type=int)
    result.add_argument("--trace", help="Exact trace ID; does not apply time/kind/state filters.")
    result.add_argument("--journal-unit", help="Optional explicit systemd unit; no service is started.")
    result.add_argument("--export", type=Path, help="Write full evidence to a NEW JSON file; never overwrite.")
    return result


def main(argv=None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.trace and any((args.since, args.until, args.kind, args.state)):
        arguments.error("--trace cannot be combined with time, kind or state filters.")
    if args.journal_unit and not args.export:
        arguments.error("--journal-unit requires --export.")
    if args.user_id is not None and args.user_id < 0:
        arguments.error("--user-id must be non-negative.")
    if args.export and args.export.exists():
        arguments.error("Export path already exists; refusing to overwrite.")
    try:
        report = build_report(args)
        safe_report = trace.sanitize_evidence(report)
        if args.export:
            descriptor = os.open(args.export, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(safe_report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            print(json.dumps({
                "export": str(args.export.resolve()), "runs": len(report["runs"]),
                "more_candidates": report["more_candidates"],
                "journal_status": report["journal"]["status"],
            }, ensure_ascii=False))
        else:
            print(json.dumps(safe_report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(trace.serialize({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
