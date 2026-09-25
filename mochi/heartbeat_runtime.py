"""Durable daily scheduling and single-attempt delivery for Free Time."""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from mochi.config import TZ
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
FREE_TIME_AWAKE_END = time(21, 0)
FREE_TIME_ACTIVATION_CHANCE = 0.6
FREE_TIME_MISSED_GRACE = timedelta(seconds=45)
_LEASE_SECONDS = 300
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


def _day_prefix(now: datetime) -> str:
    return f"free_time:{now.astimezone(TZ).date().isoformat()}:%"


def ensure_daily_free_time_plan(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    max_daily: int,
    awake: bool,
    wake_hour: int = 6,
    sleep_hour: int = 23,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Persist the day's random opportunities; setting changes share its budget."""
    rng = rng or random.SystemRandom()
    local_now = now.astimezone(TZ)
    local_date = local_now.date().isoformat()
    now_iso = _iso(now)
    max_daily = max(0, min(10, int(max_daily)))
    start = local_now.replace(hour=wake_hour, minute=0, second=0, microsecond=0)
    end = datetime.combine(
        local_now.date(), min(FREE_TIME_AWAKE_END, time(sleep_hour)), tzinfo=TZ,
    )
    if not awake or not start <= local_now < end:
        return []
    marker = (
        f"{start.isoformat()}:{end.isoformat()}:"
        f"{max_daily}:{user_id}:{channel_id}:{transport}"
    )
    start = local_now
    created: list[str] = []
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        plan = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'",
        ).fetchone()
        if plan is not None and plan["wake_reason"] == marker:
            conn.commit()
            return created
        conn.execute(
            "UPDATE heartbeat_runs SET status = 'expired', outcome = 'plan_replaced', "
            "handled_at = ?, next_attempt_at = NULL "
            "WHERE entry_kind = 'free_time' AND user_id = ? AND run_key LIKE ? "
            "AND status = 'pending'",
            (now_iso, user_id, _day_prefix(now)),
        )
        consumed = conn.execute(
            "SELECT COUNT(*) FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND user_id = ? AND run_key LIKE ? "
            "AND attempt_count > 0",
            (user_id, _day_prefix(now)),
        ).fetchone()[0]
        remaining = max(0, max_daily - int(consumed))
        if local_now < end:
            window_seconds = (end - start).total_seconds()
            for ordinal in range(remaining):
                if rng.random() >= FREE_TIME_ACTIVATION_CHANCE:
                    continue
                due = start + timedelta(seconds=rng.random() * window_seconds)
                due_iso = _iso(due)
                run_key = f"free_time:{local_date}:{ordinal}:{due_iso}"
                conn.execute(
                    "INSERT OR IGNORE INTO heartbeat_runs "
                    "(run_key, entry_kind, user_id, channel_id, transport, "
                    "wake_reason, status, next_attempt_at, created_at) "
                    "VALUES (?, 'free_time', ?, ?, ?, 'daily_random', "
                    "'pending', ?, ?)",
                    (run_key, user_id, channel_id, transport, due_iso, now_iso),
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    created.append(run_key)
        conn.execute(
            "INSERT INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) "
            "VALUES ('free_time_plan', ?, ?, ?) "
            "ON CONFLICT(entry_kind) DO UPDATE SET "
            "next_due_at = excluded.next_due_at, wake_reason = excluded.wake_reason, "
            "updated_at = excluded.updated_at",
            (_iso(end), marker, now_iso),
        )
        conn.commit()
        return created
    finally:
        conn.close()


def expire_unusable_free_time_runs(
    *, now: datetime, active_chat: bool, awake: bool, enabled: bool = True,
) -> int:
    """Consume missed or currently blocked opportunities without a catch-up turn."""
    now_iso = _iso(now)
    cutoff = _iso(now.astimezone(UTC) - FREE_TIME_MISSED_GRACE)
    due_before = now_iso if active_chat or not awake or not enabled else cutoff
    outcome = "active_chat" if active_chat else "asleep" if not awake else "expired"
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'expired', outcome = ?, "
            "handled_at = ?, next_attempt_at = NULL "
            "WHERE entry_kind = 'free_time' AND status = 'pending' "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?)",
            (outcome, now_iso, due_before),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_schedulable_runs(*, now: datetime) -> list[dict]:
    now_iso = _iso(now)
    cutoff = _iso(now.astimezone(UTC) - FREE_TIME_MISSED_GRACE)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE entry_kind = 'free_time' "
            "AND status = 'pending' AND next_attempt_at > ? "
            "AND next_attempt_at <= ? ORDER BY next_attempt_at, run_key",
            (cutoff, now_iso),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


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
            "WHERE status IN ('ready', 'running') "
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
    run_key: str, *, max_daily: int, now: datetime | None = None,
    lease_seconds: int = _LEASE_SECONDS,
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
        due = _as_utc(item.get("next_attempt_at"))
        if (
            status != "pending" or item.get("result_json")
            or item.get("attempt_count") or item.get("claim_token")
            or item["entry_kind"] != "free_time"
            or due is None or not now - FREE_TIME_MISSED_GRACE < due <= now
        ):
            conn.rollback()
            return None
        consumed = conn.execute(
            "SELECT COUNT(*) FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND user_id = ? AND run_key LIKE ? "
            "AND attempt_count > 0",
            (item["user_id"], _day_prefix(now)),
        ).fetchone()[0]
        if consumed >= max(0, min(10, int(max_daily))):
            conn.rollback()
            return None
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = 'running', claim_token = ?, lease_until = ?, "
            "attempt_count = attempt_count + 1, "
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
            attempt_count=1,
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
        "chat_generation": claimed.get("_chat_generation"),
        "state_changed_at": claimed.get("_state_changed_at"),
    }
    if claimed["entry_kind"] != "free_time":
        raise ValueError(f"Unsupported heartbeat entry: {claimed['entry_kind']}")
    return MainRuntimeEntry.free_time(**common)


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
    if outcome not in {"skip", "tools_only", "suppressed", "expired", "active_chat"}:
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
            "handled_at = ?, "
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
