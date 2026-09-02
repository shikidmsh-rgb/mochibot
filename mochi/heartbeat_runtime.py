"""Durable scheduling, observer facts, and delivery state for Main heartbeat entries."""

from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone

from mochi.config import (
    FREE_TIME_MIN_GAP_MINUTES,
    FREE_TIME_SEARCH_SHARE,
    FREE_TIME_UNAVAILABLE_FLOOR_MINUTES,
    TZ,
    logical_today,
)
from mochi.db import _connect, get_tool_executions_for_turn
from mochi.main_runtime import AttentionFact, DurableChatResult, MainRuntimeEntry


UTC = timezone.utc
_LEASE_SECONDS = 300
_MAX_ATTENTION_FACTS = 12


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


# Default window; live bounds come from system config and may wrap midnight.
FREE_TIME_AWAKE_START = time(8, 0)
FREE_TIME_AWAKE_END = time(0, 0)
FREE_TIME_MISSED_GRACE = timedelta(seconds=45)
# Opportunities are spread over even buckets and jittered inside them. The
# margin keeps consecutive slots at least 2*margin*bucket apart, so a 30s
# heartbeat tick can still claim each one within FREE_TIME_MISSED_GRACE.
FREE_TIME_JITTER_MARGIN = 0.2
_UNAVAILABLE_NEG = re.compile(r"(睡不着|失眠|还不睡|没睡|不忙|没在忙|不困)")
_SLEEP_CUE = re.compile(
    r"(我睡了|去睡了|睡觉了|先睡了|要睡了|去睡觉|先去睡|睡啦|晚安)",
)
_BUSY_CUE = re.compile(
    r"(在忙|忙着|去忙|先忙|有点忙|我忙|开会|上课|"
    r"回头聊|回头再说|别吵|别烦我|别找我)",
)


def _day_prefix(local_date: str) -> str:
    return f"free_time:{local_date}:%"


def try_parse_clock_time(value: object) -> time | None:
    """Parse ``HH:MM``; return None when the value is not a valid clock."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute)


def parse_clock_time(value: object, fallback: time) -> time:
    """Parse ``HH:MM``; invalid values fall back instead of raising."""
    parsed = try_parse_clock_time(value)
    return parsed if parsed is not None else fallback


def format_clock_time(value: time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def free_time_awake_clock() -> tuple[time, time]:
    """Return the configured Free Time window, falling back to defaults."""
    fallback_start = FREE_TIME_AWAKE_START
    fallback_end = FREE_TIME_AWAKE_END
    try:
        from mochi.admin.admin_db import get_system_config

        start = parse_clock_time(
            get_system_config("FREE_TIME_AWAKE_START"), fallback_start,
        )
        end = parse_clock_time(
            get_system_config("FREE_TIME_AWAKE_END"), fallback_end,
        )
    except Exception:
        start, end = fallback_start, fallback_end
    if start == end:
        return fallback_start, fallback_end
    return start, end


def free_time_clock_capacity(start: time, end: time) -> int:
    """How many min-gap slots fit in the clock window, wrapping if needed."""
    if start == end:
        return 0
    anchor = datetime(2000, 1, 1, tzinfo=timezone.utc)
    begin = datetime.combine(anchor.date(), start, tzinfo=timezone.utc)
    if start < end:
        finish = datetime.combine(anchor.date(), end, tzinfo=timezone.utc)
    else:
        finish = datetime.combine(
            anchor.date() + timedelta(days=1), end, tzinfo=timezone.utc,
        )
    return max_free_time_slots(begin, finish)


def free_time_plan_bounds(now: datetime) -> tuple[str, datetime, datetime]:
    """Return ``(plan_date, start, end)`` for the configured awake window.

    Same-calendar-day windows stay on today. Windows that wrap midnight keep
    hours before the end clock on yesterday's plan.
    """
    start_clock, end_clock = free_time_awake_clock()
    local_now = now.astimezone(TZ)
    wraps = start_clock >= end_clock
    if wraps and local_now.time() < end_clock:
        plan_date = local_now.date() - timedelta(days=1)
    else:
        plan_date = local_now.date()
    start = datetime.combine(plan_date, start_clock, tzinfo=TZ)
    if wraps:
        end = datetime.combine(
            plan_date + timedelta(days=1), end_clock, tzinfo=TZ,
        )
    else:
        end = datetime.combine(plan_date, end_clock, tzinfo=TZ)
    return plan_date.isoformat(), start, end


def in_free_time_window(now: datetime) -> bool:
    local_now = now.astimezone(TZ)
    _plan_date, start, end = free_time_plan_bounds(local_now)
    return start <= local_now < end


def max_free_time_slots(start: datetime, end: datetime) -> int:
    """How many points fit in ``[start, end)`` with the configured min gap."""
    gap = timedelta(minutes=int(FREE_TIME_MIN_GAP_MINUTES))
    span = end - start
    if span <= timedelta(0):
        return 0
    count = int(span // gap)
    if count < 1:
        return 1 if span > timedelta(seconds=1) else 0
    while count > 1 and start + gap * (count - 1) >= end:
        count -= 1
    return count


def plan_free_time_slot_times(
    start: datetime,
    end: datetime,
    count: int,
    rng: random.Random | random.SystemRandom,
) -> list[datetime]:
    """Even buckets across the full window, jittered, never closer than min gap."""
    gap = timedelta(minutes=int(FREE_TIME_MIN_GAP_MINUTES))
    count = max(0, min(int(count), max_free_time_slots(start, end)))
    if count == 0 or start >= end:
        return []
    span = end - start

    def _even_centers() -> list[datetime]:
        bucket = span / count
        return [start + bucket * (index + 0.5) for index in range(count)]

    if count == 1:
        margin = min(FREE_TIME_JITTER_MARGIN, 0.4)
        due = start + span * (margin + rng.random() * (1 - 2 * margin))
        if due < start:
            due = start
        if due >= end:
            due = end - timedelta(seconds=1)
        return [due]

    bucket = span / count
    needed_margin = (gap / (2 * bucket)) if bucket > timedelta(0) else 1.0
    if needed_margin >= 0.5:
        return _even_centers()
    margin = min(0.49, max(FREE_TIME_JITTER_MARGIN, float(needed_margin)))
    jitter_span = 1 - 2 * margin
    dues = [
        start + bucket * (index + margin + rng.random() * jitter_span)
        for index in range(count)
    ]
    dues.sort()
    for index, due in enumerate(dues):
        if due < start:
            dues[index] = start
        if index and dues[index] - dues[index - 1] < gap:
            dues[index] = dues[index - 1] + gap
    if dues[-1] >= end:
        dues[-1] = end - timedelta(seconds=1)
        for index in range(len(dues) - 2, -1, -1):
            latest = dues[index + 1] - gap
            if dues[index] > latest:
                dues[index] = latest
        if dues[0] < start:
            return _even_centers()
    return dues


def _unavailable_floor() -> timedelta:
    minutes = max(1, int(FREE_TIME_UNAVAILABLE_FLOOR_MINUTES))
    return timedelta(minutes=minutes)


def unavailable_cue_from_text(text: str) -> str | None:
    """Return sleep/busy if the last owner line is a short status ping."""
    compact = re.sub(r"\s+", "", (text or "").strip())
    if not compact or len(compact) > 80:
        return None
    if _UNAVAILABLE_NEG.search(compact):
        return None
    if _SLEEP_CUE.search(compact):
        return "sleep"
    if _BUSY_CUE.search(compact):
        return "busy"
    return None


def owner_free_time_unavailable_cue(
    *,
    sleeping: bool,
    last_user_text: str | None,
    owner_spoken_since_wake: bool = True,
) -> str | None:
    if sleeping:
        return "sleep"
    if not owner_spoken_since_wake:
        return "quiet_wake"
    return unavailable_cue_from_text(last_user_text or "")


def last_delivered_free_time_at(user_id: int) -> datetime | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT text_delivered_at FROM heartbeat_runs "
            "WHERE entry_kind = 'free_time' AND user_id = ? "
            "AND text_delivered_at IS NOT NULL "
            "ORDER BY text_delivered_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    return _as_utc(row["text_delivered_at"]) if row else None


def should_skip_unavailable_slot(
    *,
    now: datetime,
    cue: str | None,
    last_delivered_at: datetime | None,
    floor: timedelta | None = None,
    since: datetime | None = None,
) -> str | None:
    """Skip a due slot during busy/sleep/quiet-wake unless the floor elapsed.

    A skip still consumes the scheduled run. The floor is measured from the
    last Free Time that actually reached the owner, not from skipped slots.
    ``quiet_wake`` also anchors to ``since`` (sleep-end) so a morning with
    no prior delivery still damps instead of firing the first due slot.
    """
    if cue not in {"sleep", "busy", "quiet_wake"}:
        return None
    gap = floor if floor is not None else _unavailable_floor()
    now_utc = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    anchors: list[datetime] = []
    if last_delivered_at is not None:
        anchors.append(last_delivered_at)
    if cue == "quiet_wake" and since is not None:
        anchors.append(since)
    if not anchors:
        return None
    last_utc = max(
        (
            value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            for value in anchors
        ),
        default=None,
    )
    if last_utc is None or now_utc - last_utc >= gap:
        return None
    return cue


def _select_search_run_keys(
    run_keys: list[str],
    rng: random.Random | random.SystemRandom,
) -> set[str]:
    share = min(1.0, max(0.0, float(FREE_TIME_SEARCH_SHARE)))
    count = min(len(run_keys), int(len(run_keys) * share + 0.5))
    return set(rng.sample(run_keys, count)) if count else set()


def ensure_daily_free_time_plan(
    *,
    user_id: int,
    channel_id: int,
    transport: str,
    now: datetime,
    max_daily: int,
    rng: random.Random | random.SystemRandom | None = None,
) -> list[str]:
    """Persist today's random Free Time opportunities exactly once."""
    rng = rng or random.SystemRandom()
    local_now = now.astimezone(TZ)
    local_date, start, end = free_time_plan_bounds(local_now)
    now_iso = _iso(local_now)
    start_clock, end_clock = free_time_awake_clock()
    capacity = max_free_time_slots(start, end)
    max_daily = max(0, min(capacity, int(max_daily)))
    share = min(1.0, max(0.0, float(FREE_TIME_SEARCH_SHARE)))
    gap_minutes = int(FREE_TIME_MIN_GAP_MINUTES)
    tz_hours = (local_now.utcoffset() or timedelta(0)).total_seconds() / 3600.0
    marker = (
        f"{local_date}:{max_daily}:{user_id}:{channel_id}:{transport}:"
        f"{format_clock_time(start_clock)}-{format_clock_time(end_clock)}:"
        f"tz={tz_hours:g}:search={share:.4f}:gap={gap_minutes}"
    )
    created: list[str] = []

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = 'expired', "
            "handled_at = ?, claim_token = NULL, lease_until = NULL, "
            "next_attempt_at = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' "
            "AND status IN ('pending', 'running', 'ready') "
            "AND run_key NOT LIKE ?",
            (now_iso, _day_prefix(local_date)),
        )
        conn.execute(
            "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time'"
        )
        plan = conn.execute(
            "SELECT wake_reason FROM heartbeat_schedules "
            "WHERE entry_kind = 'free_time_plan'"
        ).fetchone()
        if plan is not None and plan["wake_reason"] == marker:
            conn.commit()
            return created

        conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', "
            "outcome = 'plan_replaced', handled_at = ?, next_attempt_at = NULL "
            "WHERE entry_kind = 'free_time' AND run_key LIKE ? "
            "AND status = 'pending'",
            (now_iso, _day_prefix(local_date)),
        )
        conn.execute(
            "DELETE FROM heartbeat_schedules WHERE entry_kind = 'free_time_plan'"
        )

        planned: list[tuple[str, datetime]] = []
        now_utc = local_now.astimezone(UTC)
        for ordinal, due_local in enumerate(
            plan_free_time_slot_times(start, end, max_daily, rng)
        ):
            due_utc = due_local.astimezone(UTC)
            run_key = (
                f"free_time:{local_date}:{ordinal}:"
                f"{due_utc.isoformat()}"
            )
            planned.append((run_key, due_utc))

        future_keys = [
            run_key for run_key, due in planned if due > now_utc
        ]
        search_keys = _select_search_run_keys(future_keys, rng)
        for run_key, due in planned:
            already_past = due <= now_utc
            conn.execute(
                "INSERT OR IGNORE INTO heartbeat_runs "
                "(run_key, entry_kind, user_id, channel_id, transport, "
                "wake_reason, facts_json, status, outcome, next_attempt_at, "
                "handled_at, created_at) "
                "VALUES (?, 'free_time', ?, ?, ?, 'daily_random', ?, "
                "?, ?, ?, ?, ?)",
                (
                    run_key,
                    user_id,
                    channel_id,
                    transport,
                    json.dumps(
                        {"direct_search": run_key in search_keys},
                        separators=(",", ":"),
                    ),
                    "delivered" if already_past else "pending",
                    "expired" if already_past else None,
                    None if already_past else _iso(due),
                    now_iso if already_past else None,
                    now_iso,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                created.append(run_key)

        conn.execute(
            "INSERT INTO heartbeat_schedules "
            "(entry_kind, next_due_at, wake_reason, updated_at) "
            "VALUES ('free_time_plan', ?, ?, ?)",
            (_iso(end.astimezone(UTC)), marker, now_iso),
        )
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def expire_unusable_free_time_runs(
    *,
    now: datetime,
    active_chat: bool,
    awake: bool,
) -> int:
    """Expire missed, sleeping, or chat-conflicting opportunities."""
    now_iso = _iso(now)
    cutoff = _iso(now.astimezone(UTC) - FREE_TIME_MISSED_GRACE)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        due_before = now_iso if active_chat or not awake else cutoff
        outcome = "active_chat" if active_chat else "asleep" if not awake else "expired"
        expired = conn.execute(
            "UPDATE heartbeat_runs SET status = 'delivered', outcome = ?, "
            "handled_at = ?, next_attempt_at = NULL, claim_token = NULL, "
            "lease_until = NULL, last_error = '' "
            "WHERE entry_kind = 'free_time' AND status = 'pending' "
            "AND next_attempt_at <= ?",
            (outcome, now_iso, due_before),
        ).rowcount
        # Only drop in-flight Free Time when the owner is chatting or asleep.
        # Prepared ready rows must remain claimable for delivery retry.
        interrupted = 0
        if active_chat or not awake:
            interrupted = conn.execute(
                "UPDATE heartbeat_runs SET status = 'delivered', "
                "outcome = 'interrupted', handled_at = ?, next_attempt_at = NULL, "
                "claim_token = NULL, lease_until = NULL, last_error = '' "
                "WHERE entry_kind = 'free_time' AND status IN ('running', 'ready') "
                "AND (lease_until IS NULL OR lease_until <= ?)",
                (now_iso, now_iso),
            ).rowcount
        conn.commit()
        return expired + interrupted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    rng: random.Random | random.SystemRandom | None = None,
) -> None:
    rng = rng or random.SystemRandom()
    now = now.astimezone(UTC)
    # Interval Free Time is replaced by ensure_daily_free_time_plan.
    rows = (
        ("attention", now + timedelta(minutes=attention_interval_minutes)),
    )
    conn = _connect()
    try:
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
            "WHERE next_due_at <= ? ORDER BY next_due_at, entry_kind",
            (now_iso,),
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


def get_schedulable_runs(*, now: datetime) -> list[dict]:
    now_iso = _iso(now)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM heartbeat_runs WHERE "
            "(status = 'ready' AND last_error = 'delivery budget/cooldown' AND "
            "(lease_until IS NULL OR lease_until <= ?)) OR "
            "(status IN ('pending', 'ready') AND "
            "(next_attempt_at IS NULL OR next_attempt_at <= ?) AND "
            "(lease_until IS NULL OR lease_until <= ?)) OR "
            "(status = 'running' AND lease_until <= ?) "
            "ORDER BY created_at, run_key",
            (now_iso, now_iso, now_iso, now_iso),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def claim_run(
    run_key: str, *, now: datetime, lease_seconds: int = _LEASE_SECONDS,
) -> dict | None:
    now = now.astimezone(UTC)
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
        retry_at = _as_utc(item.get("next_attempt_at"))
        lease = _as_utc(item.get("lease_until"))
        legacy_budget_queue = (
            status == "ready"
            and item.get("last_error") == "delivery budget/cooldown"
        )
        if status not in {"pending", "ready", "running"}:
            conn.rollback()
            return None
        if retry_at and retry_at > now and not legacy_budget_queue:
            conn.rollback()
            return None
        if lease and lease > now:
            conn.rollback()
            return None
        claimed_status = "ready" if item.get("result_json") else "running"
        cursor = conn.execute(
            "UPDATE heartbeat_runs SET status = ?, claim_token = ?, lease_until = ?, "
            "delivery_started_at = NULL WHERE run_key = ? AND status = ?",
            (claimed_status, claim_token, lease_until, run_key, status),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        item.update(
            status=claimed_status,
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
    if outcome not in {
        "skip",
        "tools_only",
        "suppressed",
        "skipped_busy",
        "skipped_sleep",
        "skipped_quiet_wake",
    }:
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
            "AND delivery_started_at IS NULL",
            (
                now_iso,
                claimed["run_key"],
                claimed["claim_token"],
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


def record_failure(claimed: dict, error: str) -> datetime | None:
    now = _utc_now()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT attempt_count, result_json FROM heartbeat_runs "
            "WHERE run_key = ? AND claim_token = ? "
            "AND status IN ('running', 'ready')",
            (claimed["run_key"], claimed["claim_token"]),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        attempt = int(row["attempt_count"] or 0) + 1
        retry_at = now + timedelta(seconds=min(60 * (2 ** min(attempt - 1, 6)), 3600))
        status = "ready" if row["result_json"] else "pending"
        conn.execute(
            "UPDATE heartbeat_runs SET status = ?, attempt_count = ?, "
            "next_attempt_at = ?, last_error = ?, claim_token = NULL, "
            "lease_until = NULL, delivery_started_at = NULL "
            "WHERE run_key = ? AND claim_token = ?",
            (
                status, attempt, _iso(retry_at), error[:1000],
                claimed["run_key"], claimed["claim_token"],
            ),
        )
        conn.commit()
        return retry_at
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
