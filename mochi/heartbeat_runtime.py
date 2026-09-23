"""Durable scheduling, observer facts, and delivery state for Main heartbeat entries."""

from __future__ import annotations

import json
import logging
import random
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from mochi.config import TZ, logical_today
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.main_runtime import AttentionFact, DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
_LEASE_SECONDS = 300
_MAX_ATTENTION_FACTS = 12
log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _as_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(UTC)


def sync_attention_facts(
    source: str,
    facts: list[dict],
    *,
    observed_at: datetime,
    freshness_seconds: int,
) -> bool:
    """Replace one source's unresolved fact set after a truthful fresh observation."""
    observed_iso = _iso(observed_at)
    fresh_until = _iso(observed_at + timedelta(seconds=max(60, freshness_seconds)))
    normalized: dict[str, str] = {}
    for fact in facts[:_MAX_ATTENTION_FACTS]:
        stable_key = str(fact.get("stable_key") or "").strip()
        payload = fact.get("facts")
        if not stable_key or not isinstance(payload, dict):
            continue
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        if len(encoded) > 2000:
            continue
        normalized[stable_key] = encoded

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior_rows = conn.execute(
            "SELECT stable_key, facts_json FROM attention_facts "
            "WHERE source = ? AND status = 'unresolved'",
            (source,),
        ).fetchall()
        prior = {row["stable_key"]: row["facts_json"] for row in prior_rows}
        changed = prior != normalized
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            conn.execute(
                "UPDATE attention_facts SET status = 'resolved', updated_at = ? "
                f"WHERE source = ? AND status = 'unresolved' "
                f"AND stable_key NOT IN ({placeholders})",
                (observed_iso, source, *normalized),
            )
        else:
            conn.execute(
                "UPDATE attention_facts SET status = 'resolved', updated_at = ? "
                "WHERE source = ? AND status = 'unresolved'",
                (observed_iso, source),
            )
        for stable_key, encoded in normalized.items():
            conn.execute(
                "INSERT INTO attention_facts "
                "(source, stable_key, observed_at, fresh_until, status, facts_json, updated_at) "
                "VALUES (?, ?, ?, ?, 'unresolved', ?, ?) "
                "ON CONFLICT(source, stable_key) DO UPDATE SET "
                "observed_at = excluded.observed_at, fresh_until = excluded.fresh_until, "
                "status = 'unresolved', facts_json = excluded.facts_json, "
                "updated_at = excluded.updated_at",
                (
                    source, stable_key, observed_iso, fresh_until,
                    encoded, observed_iso,
                ),
            )
        conn.commit()
        return changed
    finally:
        conn.close()


def get_unresolved_attention_facts(
    *, now: datetime | None = None, limit: int = _MAX_ATTENTION_FACTS,
) -> tuple[AttentionFact, ...]:
    now = (now or _utc_now()).astimezone(UTC)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT source, stable_key, observed_at, fresh_until, facts_json "
            "FROM attention_facts WHERE status = 'unresolved' "
            "ORDER BY observed_at DESC, source, stable_key LIMIT ?",
            (max(1, min(limit, _MAX_ATTENTION_FACTS)),),
        ).fetchall()
    finally:
        conn.close()
    return tuple(
        AttentionFact(
            source=row["source"],
            stable_key=row["stable_key"],
            observed_at=row["observed_at"],
            freshness=(
                "fresh"
                if (_as_utc(row["fresh_until"]) or now) >= now
                else "stale"
            ),
            status="unresolved",
            facts=json.loads(row["facts_json"]),
        )
        for row in rows
    )


def ensure_schedules(
    *,
    now: datetime,
    attention_interval_minutes: int,
    free_time_min_minutes: int,
    free_time_max_minutes: int,
    free_time_enabled: bool = True,
    rng: random.Random | random.SystemRandom | None = None,
) -> None:
    rng = rng or random.SystemRandom()
    now = now.astimezone(UTC)
    rows = [
        ("attention", now + timedelta(minutes=attention_interval_minutes)),
    ]
    if free_time_enabled:
        free_delay = rng.randint(
            min(free_time_min_minutes, free_time_max_minutes),
            max(free_time_min_minutes, free_time_max_minutes),
        )
        rows.append(("free_time", now + timedelta(minutes=free_delay)))
    conn = _connect()
    try:
        if not free_time_enabled:
            conn.execute(
                "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time'",
            )
        for kind, due in rows:
            conn.execute(
                "INSERT OR IGNORE INTO heartbeat_schedules "
                "(entry_kind, next_due_at, wake_reason, updated_at) "
                "VALUES (?, ?, 'periodic', ?)",
                (kind, _iso(due), _iso(now)),
            )
        conn.commit()
    finally:
        conn.close()


def advance_attention(*, now: datetime, wake_reason: str = "observer_change") -> None:
    now_iso = _iso(now)
    conn = _connect()
    try:
        conn.execute(
            "UPDATE heartbeat_schedules SET "
            "next_due_at = CASE WHEN next_due_at > ? THEN ? ELSE next_due_at END, "
            "wake_reason = ?, updated_at = ? WHERE entry_kind = 'attention'",
            (now_iso, now_iso, wake_reason, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def set_schedule_due(
    kind: str, due_at: datetime, *, wake_reason: str = "periodic",
) -> None:
    """Set one clock directly; primarily useful for deterministic tests."""
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(entry_kind) DO UPDATE SET next_due_at = excluded.next_due_at, "
            "wake_reason = excluded.wake_reason, updated_at = excluded.updated_at",
            (kind, _iso(due_at), wake_reason, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def materialize_due_runs(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    attention_interval_minutes: int,
    free_time_min_minutes: int,
    free_time_max_minutes: int,
    free_time_enabled: bool = True,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Snapshot all due independent clocks and advance each one atomically."""
    rng = rng or random.SystemRandom()
    now = now.astimezone(UTC)
    now_iso = _iso(now)
    facts = get_unresolved_attention_facts(now=now)
    facts_json = json.dumps(
        [
            {
                "source": fact.source,
                "stable_key": fact.stable_key,
                "observed_at": fact.observed_at,
                "freshness": fact.freshness,
                "status": fact.status,
                "facts": fact.facts,
            }
            for fact in facts
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    created: list[str] = []
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due_rows = conn.execute(
            "SELECT entry_kind, next_due_at, wake_reason FROM heartbeat_schedules "
            "WHERE (entry_kind = 'attention' OR (entry_kind = 'free_time' AND ?)) "
            "AND next_due_at <= ? "
            "ORDER BY next_due_at, entry_kind",
            (free_time_enabled, now_iso),
        ).fetchall()
        for row in due_rows:
            kind = row["entry_kind"]
            due_at = row["next_due_at"]
            run_key = f"{kind}:{due_at}"
            payload = facts_json if kind == "attention" else "[]"
            if kind != "attention" or facts:
                conn.execute(
                    "INSERT OR IGNORE INTO heartbeat_runs "
                    "(run_key, entry_kind, user_id, channel_id, transport, wake_reason, "
                    "facts_json, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        run_key, kind, user_id, channel_id, transport,
                        row["wake_reason"], payload, now_iso,
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    created.append(run_key)
            if kind == "attention":
                next_due = now + timedelta(minutes=attention_interval_minutes)
            else:
                delay = rng.randint(
                    min(free_time_min_minutes, free_time_max_minutes),
                    max(free_time_min_minutes, free_time_max_minutes),
                )
                next_due = now + timedelta(minutes=delay)
            conn.execute(
                "UPDATE heartbeat_schedules SET next_due_at = ?, "
                "wake_reason = 'periodic', updated_at = ? WHERE entry_kind = ?",
                (_iso(next_due), now_iso, kind),
            )
        conn.commit()
    finally:
        conn.close()
    return created


def expire_abandoned_runs(*, now: datetime | None = None) -> int:
    """Retain abandoned turns for audit, never replay their text or tools."""
    now_iso = _iso(now or _utc_now())
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'expired', "
            "outcome = CASE WHEN delivery_started_at IS NOT NULL "
            "THEN 'delivery_unknown' ELSE 'expired' END, handled_at = ?, "
            "claim_token = NULL, lease_until = NULL, next_attempt_at = NULL "
            "WHERE status IN ('pending', 'ready', 'running') "
            "AND (lease_until IS NULL OR lease_until <= ?)",
            (now_iso, now_iso),
        )
        conn.commit()
        if cursor.rowcount:
            log.info("Expired %d abandoned autonomous turns", cursor.rowcount)
        return cursor.rowcount
    finally:
        conn.close()


def claim_run(
    run_key: str, *, now: datetime | None = None, lease_seconds: int = _LEASE_SECONDS,
) -> dict | None:
    now = (now or _utc_now()).astimezone(UTC)
    now_iso = _iso(now)
    claim_token = f"{now_iso}:{uuid.uuid4().hex}"
    lease_until = _iso(now + timedelta(seconds=lease_seconds))
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE run_key = ?", (run_key,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        item = dict(row)
        status = item["status"]
        if (
            status != "pending" or item.get("result_json")
            or item.get("attempt_count") or item.get("claim_token")
        ):
            conn.rollback()
            return None
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'running', claim_token = ?, lease_until = ?, "
            "delivery_started_at = NULL WHERE run_key = ? AND status = ?",
            (claim_token, lease_until, run_key, status),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        item.update(
            status="running",
            claim_token=claim_token,
            lease_until=lease_until,
        )
        return item
    finally:
        conn.close()


def entry_from_claim(claimed: dict) -> MainRuntimeEntry:
    common = {
        "run_key": claimed["run_key"],
        "wake_reason": claimed["wake_reason"],
        "user_id": claimed["user_id"],
        "channel_id": claimed["channel_id"],
        "transport": claimed["transport"],
        "claim_token": claimed["claim_token"],
        "lease_until": claimed["lease_until"],
    }
    if claimed["entry_kind"] == "free_time":
        return MainRuntimeEntry.free_time(**common)
    if claimed["entry_kind"] != "attention":
        raise ValueError(f"Unsupported heartbeat entry: {claimed['entry_kind']}")
    raw_facts = json.loads(claimed.get("facts_json") or "[]")
    facts = tuple(AttentionFact(**item) for item in raw_facts)
    return MainRuntimeEntry.attention(facts=facts, **common)


def store_prepared_result(claimed: dict, durable: DurableChatResult) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'ready', result_json = ?, "
            "outcome = 'ready', last_error = '' WHERE run_key = ? "
            "AND status = 'running' AND claim_token = ?",
            (durable.to_json(), claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def complete_without_delivery(
    claimed: dict, durable: DurableChatResult, outcome: str,
) -> bool:
    if outcome not in {"skip", "tools_only", "suppressed", "expired"}:
        raise ValueError("invalid autonomous Main outcome")
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', result_json = ?, "
            "outcome = ?, handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' WHERE run_key = ? "
            "AND status IN ('running', 'ready') AND claim_token = ?",
            (
                durable.to_json(), outcome, now_iso,
                claimed["run_key"], claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def recover_prior_tool_attempt(claimed: dict) -> DurableChatResult | None:
    executions = get_tool_executions_for_turn(claimed["run_key"])
    if not executions:
        return None
    return DurableChatResult(
        tool_audit=tuple(
            {
                "name": item["tool_name"],
                "status": item["status"],
                "state_changed": bool(item["state_changed"]),
            }
            for item in executions
        ),
        successful_effects=any(
            item["status"] == "success" and item["state_changed"]
            for item in executions
        ),
        disposition="handled",
    )


def delivery_wait_seconds(
    *,
    now: datetime,
    max_daily: int,
    cooldown_seconds: int,
) -> int:
    from mochi.admin.admin_db import get_system_config

    local_now = now.astimezone(TZ)
    day = logical_today(local_now)
    start = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=get_system_config("MAINTENANCE_HOUR"), tzinfo=TZ,
    ).astimezone(UTC)
    end = start + timedelta(days=1)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS count, MAX(text_delivered_at) AS latest "
            "FROM heartbeat_runs WHERE text_delivered_at >= ? "
            "AND text_delivered_at < ?",
            (_iso(start), _iso(end)),
        ).fetchone()
    finally:
        conn.close()
    if int(row["count"] or 0) >= max_daily:
        return max(1, int((end - now.astimezone(UTC)).total_seconds()))
    latest = _as_utc(row["latest"])
    if latest:
        remaining = cooldown_seconds - int(
            (now.astimezone(UTC) - latest).total_seconds()
        )
        if remaining > 0:
            return remaining
    return 0


def delivery_lease_valid(claimed: dict) -> bool:
    deadline = _as_utc(claimed.get("lease_until"))
    return deadline is not None and _utc_now() < deadline


def begin_delivery(
    claimed: dict,
    *,
    now: datetime | None = None,
) -> bool:
    now_iso = _iso(now or _utc_now())
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET delivery_started_at = ? "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NULL AND lease_until > ?",
            (
                now_iso,
                claimed["run_key"],
                claimed["claim_token"],
                now_iso,
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
def store_delivery_progress(claimed: dict, remaining: DurableChatResult) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET result_json = ? WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NOT NULL",
            (
                remaining.to_json(),
                claimed["run_key"],
                claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def checkpoint_text_delivery(
    claimed: dict, *, content: str, entry_kind: str,
) -> bool:
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET text_delivered_at = ? WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND text_delivered_at IS NULL",
            (now_iso, claimed["run_key"], claimed["claim_token"]),
        )
        if cursor.rowcount == 1:
            conn.execute(
                "INSERT INTO proactive_log (type, content, created_at) "
                "VALUES (?, ?, ?)",
                (entry_kind, content, now_iso),
            )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def checkpoint_visible_delivery(claimed: dict) -> bool:
    """Count a sticker-only delivery without creating text history or logs."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET text_delivered_at = "
            "COALESCE(text_delivered_at, ?) WHERE run_key = ? "
            "AND status = 'ready' AND claim_token = ? "
            "AND delivery_started_at IS NOT NULL",
            (_iso(_utc_now()), claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def record_failure(
    claimed: dict, error: str, *, outcome: str = "failed",
) -> bool:
    if outcome not in {
        "failed", "delivery_unavailable", "delivery_rejected", "delivery_unknown",
    }:
        raise ValueError("invalid autonomous failure outcome")
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'failed', outcome = ?, "
            "attempt_count = attempt_count + 1, handled_at = ?, "
            "next_attempt_at = NULL, last_error = ?, claim_token = NULL, "
            "lease_until = NULL WHERE run_key = ? AND claim_token = ? "
            "AND status IN ('running', 'ready')",
            (
                outcome, _iso(_utc_now()), error[:1000],
                claimed["run_key"], claimed["claim_token"],
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def complete_delivery(claimed: dict) -> bool:
    now_iso = _iso(_utc_now())
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'delivered', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '', delivery_started_at = NULL "
            "WHERE run_key = ? AND status = 'ready' AND claim_token = ?",
            (now_iso, claimed["run_key"], claimed["claim_token"]),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def remove_delivered_component(
    durable: DurableChatResult, kind: str, value: str,
) -> DurableChatResult:
    if kind == "text":
        return replace(durable, text="")
    stickers = list(durable.stickers)
    stickers.remove(value)
    return replace(durable, stickers=tuple(stickers))
