"""Heartbeat schedules sovereign Main entries and owns no semantic judgment."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mochi.config import (
    SILENCE_THRESHOLD_HOURS,
    TZ,
    logical_today,
)
from mochi.db import (
    _connect,
    get_last_user_message_time,
    log_heartbeat,
)
from mochi.heartbeat_runtime import (
    begin_delivery,
    checkpoint_text_delivery,
    checkpoint_visible_delivery,
    claim_run,
    complete_delivery,
    complete_without_delivery,
    delivery_wait_seconds,
    ensure_schedules,
    entry_from_claim,
    get_schedulable_runs,
    materialize_due_runs,
    record_failure,
    recover_prior_tool_attempt,
    remove_delivered_component,
    store_delivery_progress,
    store_prepared_result,
)
from mochi.main_runtime import DurableChatResult


log = logging.getLogger(__name__)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".heartbeat_state"

SLEEPING = "SLEEPING"
AWAKE = "AWAKE"
TRANSITIONING = "TRANSITIONING"
RESLEEP_WINDOW_HOURS = 6


def _effective(key: str):
    from mochi.admin.admin_db import get_system_config

    return get_system_config(key)


def _wake_earliest_hour() -> int:
    return int(_effective("WAKE_EARLIEST_HOUR"))


def _sleep_after_hour() -> int:
    return int(_effective("SLEEP_AFTER_HOUR"))


def _is_awake_hour(hour: int) -> bool:
    return _wake_earliest_hour() <= hour < _sleep_after_hour()


def _is_rest_hour(hour: int) -> bool:
    return not _is_awake_hour(hour)


def _persist_state(state: str, changed_at: datetime | None = None) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = (changed_at or datetime.now(TZ)).isoformat()
        _STATE_FILE.write_text(
            json.dumps({"state": state, "at": ts}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.debug("Failed to persist heartbeat state: %s", exc)


def _init_state() -> str:
    now = datetime.now(TZ)
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            saved = data.get("state")
            saved_at = datetime.fromisoformat(data["at"])
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=TZ)
            if (
                (now - saved_at).total_seconds() < 12 * 3600
                and saved in {SLEEPING, AWAKE, TRANSITIONING}
            ):
                return SLEEPING if saved == TRANSITIONING else saved
    except Exception as exc:
        log.debug("Failed to read persisted heartbeat state: %s", exc)
    return AWAKE if _is_awake_hour(now.hour) else SLEEPING


_state: str = _init_state()
_state_changed_at: datetime = datetime.now(TZ)
_wake_reason: str | None = None
_last_sleep_at: datetime | None = None
_silent_pause = False

_bedtime_callback = None
_weekly_callback = None
_runtime_prepare_callback = None
_runtime_delivery_callback = None
_runtime_transport = ""


def reload_state_after_config_seed() -> None:
    """Resolve initial state again after .env settings enter the DB."""
    global _state, _state_changed_at
    _state = _init_state()
    _state_changed_at = datetime.now(TZ)


def set_bedtime_callback(callback) -> None:
    global _bedtime_callback
    _bedtime_callback = callback


def set_weekly_callback(callback) -> None:
    global _weekly_callback
    _weekly_callback = callback


def set_main_runtime_callbacks(prepare_callback, delivery_callback, transport: str) -> None:
    global _runtime_prepare_callback, _runtime_delivery_callback, _runtime_transport
    _runtime_prepare_callback = prepare_callback
    _runtime_delivery_callback = delivery_callback
    _runtime_transport = transport


def wake_up(reason: str = "unknown") -> None:
    global _state, _state_changed_at, _wake_reason
    if _state == SLEEPING:
        _state = AWAKE
        _state_changed_at = datetime.now(TZ)
        _wake_reason = reason
        _persist_state(AWAKE, _state_changed_at)


def go_to_sleep(reason: str = "unknown") -> None:
    global _state, _state_changed_at, _wake_reason, _last_sleep_at
    if _state in {AWAKE, TRANSITIONING}:
        _state = SLEEPING
        _state_changed_at = datetime.now(TZ)
        _last_sleep_at = _state_changed_at
        _wake_reason = None
        _persist_state(SLEEPING, _state_changed_at)
        log.info("SLEEPING - reason: %s", reason)


def claim_sleep_transition(trigger: str) -> bool:
    global _state, _state_changed_at
    if _state != AWAKE:
        return False
    _state = TRANSITIONING
    _state_changed_at = datetime.now(TZ)
    _persist_state(TRANSITIONING, _state_changed_at)
    log.info("Sleep transition claimed: %s", trigger)
    return True


def should_wake_on_message() -> bool:
    return (
        _state == SLEEPING
        and datetime.now(TZ).hour >= _wake_earliest_hour()
    )


def bedtime_tool_available() -> bool:
    if _state != AWAKE or not bedtime_entry_enabled():
        return False
    return _is_rest_hour(datetime.now(TZ).hour)


def bedtime_entry_enabled() -> bool:
    return bool(_effective("BEDTIME_ENTRY_ENABLED"))


def bedtime_entry_timeout() -> float:
    return float(_effective("BEDTIME_ENTRY_TIMEOUT_S"))


async def run_silent_bedtime(user_id: int, trigger: str) -> bool:
    if not claim_sleep_transition(trigger):
        return False
    try:
        if not bedtime_entry_enabled() or _bedtime_callback is None:
            return False
        delivered = bool(
            await asyncio.wait_for(
                _bedtime_callback(user_id, trigger),
                timeout=bedtime_entry_timeout(),
            )
        )
        if delivered:
            log_heartbeat(_state, "bedtime_entry", trigger)
        return delivered
    except asyncio.TimeoutError:
        log_heartbeat(_state, "bedtime_timeout", trigger)
        return False
    except Exception as exc:
        log.error("Bedtime entry failed: %s", exc, exc_info=True)
        log_heartbeat(_state, "bedtime_failure", str(exc)[:200])
        return False
    finally:
        go_to_sleep(f"{trigger}_detected")


def check_silence_sleep() -> dict | None:
    if _state != AWAKE:
        return None
    now = datetime.now(TZ)
    if not _is_rest_hour(now.hour):
        return None
    from mochi.config import OWNER_USER_ID as user_id

    if user_id is None:
        return None
    raw = get_last_user_message_time(user_id)
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        silence_hours = (now - last).total_seconds() / 3600
    except (TypeError, ValueError):
        return None
    if silence_hours < SILENCE_THRESHOLD_HOURS:
        return None
    is_resleep = bool(
        _last_sleep_at
        and (now - _last_sleep_at).total_seconds() < RESLEEP_WINDOW_HOURS * 3600
    )
    return {
        "context_hint": "re_sleep" if is_resleep else "first_sleep",
        "silence_hours": round(silence_hours, 1),
    }


def enter_silent_pause() -> None:
    global _silent_pause
    _silent_pause = True


def clear_silent_pause() -> None:
    global _silent_pause
    _silent_pause = False


def _check_silence_pause() -> None:
    from mochi.config import OWNER_USER_ID as user_id

    if user_id is None:
        return
    raw = get_last_user_message_time(user_id)
    if not raw:
        return
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=TZ)
        silence_hours = (datetime.now(TZ) - last).total_seconds() / 3600
    except (TypeError, ValueError):
        return
    if silence_hours >= float(_effective("SILENCE_PAUSE_DAYS")) * 24:
        enter_silent_pause()
    elif _silent_pause:
        clear_silent_pause()


def get_stats() -> dict:
    now = datetime.now(TZ)
    day = logical_today(now)
    start = datetime.strptime(day, "%Y-%m-%d").replace(
        hour=_effective("MAINTENANCE_HOUR"), tzinfo=TZ,
    ).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    conn = _connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM heartbeat_runs WHERE text_delivered_at >= ? "
            "AND text_delivered_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "state": _state,
        "state_changed_at": _state_changed_at.isoformat(),
        "proactive_today": count,
        "proactive_limit": _effective("MAX_DAILY_PROACTIVE"),
        "wake_reason": _wake_reason,
    }


async def _run_maintenance_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    if not _effective("MAINTENANCE_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    period = logical_today(now)
    if now.hour < _effective("MAINTENANCE_HOUR"):
        return False
    from mochi.db import claim_scheduled_run, finish_scheduled_run

    if not claim_scheduled_run("nightly", period):
        return False
    try:
        import mochi.skills as skill_registry
        from mochi.skills.base import SkillContext

        skill = skill_registry.get_skill("maintenance")
        if skill is None:
            raise RuntimeError("Maintenance skill not found")
        result = await skill.run(SkillContext(trigger="cron", user_id=user_id))
        if not result.success:
            raise RuntimeError(result.output or "Maintenance skill failed")
    except Exception as exc:
        finish_scheduled_run("nightly", period, success=False, error=str(exc))
        log_heartbeat(_state, "maintenance_error", str(exc)[:200])
        return True
    finish_scheduled_run("nightly", period, success=True)
    log_heartbeat(_state, "maintenance", result.output[:200])
    return True


async def _run_weekly_if_due(
    user_id: int,
    now: datetime | None = None,
) -> bool:
    if not _effective("WEEKLY_MAINTENANCE_ENABLED"):
        return False
    now = now or datetime.now(TZ)
    logical_date = logical_today(now)
    logical_day = datetime.strptime(logical_date, "%Y-%m-%d").date()
    if logical_day.weekday() != 0:
        return False
    maintenance_hour = _effective("MAINTENANCE_HOUR")
    weekly_minute = _effective("WEEKLY_MAINTENANCE_MINUTE")
    if now.hour < maintenance_hour or (
        now.hour == maintenance_hour and now.minute < weekly_minute
    ):
        return False
    from mochi.db import (
        claim_scheduled_run,
        finish_scheduled_run,
        get_scheduled_run,
    )

    nightly = get_scheduled_run("nightly", logical_date)
    if not nightly or nightly["status"] != "success":
        return False
    iso = logical_day.isocalendar()
    period_key = f"{iso.year}-W{iso.week:02d}"
    if not claim_scheduled_run("weekly", period_key):
        return False
    try:
        if _weekly_callback is None:
            raise RuntimeError("Weekly Main callback is not registered")
        await asyncio.wait_for(
            _weekly_callback(user_id, logical_date, period_key),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
    except Exception as exc:
        finish_scheduled_run("weekly", period_key, success=False, error=str(exc))
        log_heartbeat(_state, "weekly_error", str(exc)[:200])
        return True
    finish_scheduled_run("weekly", period_key, success=True)
    log_heartbeat(_state, "weekly", period_key)
    return True


async def _prepare_autonomous(claimed: dict) -> DurableChatResult | None:
    if claimed.get("result_json"):
        return DurableChatResult.from_json(claimed["result_json"])
    recovered = recover_prior_tool_attempt(claimed)
    if recovered is not None:
        complete_without_delivery(claimed, recovered, "tools_only")
        log_heartbeat(_state, f"{claimed['entry_kind']}_tools_only", "recovered")
        return None
    if _runtime_prepare_callback is None:
        record_failure(claimed, "Main runtime callback is not registered")
        return None
    try:
        result = await asyncio.wait_for(
            _runtime_prepare_callback(entry_from_claim(claimed)),
            timeout=_effective("LLM_HEARTBEAT_TIMEOUT_SECONDS"),
        )
        durable = result.to_durable()
    except asyncio.TimeoutError:
        record_failure(claimed, "Main runtime timed out")
        log_heartbeat(_state, f"{claimed['entry_kind']}_timeout")
        return None
    except Exception as exc:
        record_failure(claimed, f"Main failed: {exc}")
        log_heartbeat(
            _state, f"{claimed['entry_kind']}_failure", str(exc)[:200],
        )
        return None
    if durable.disposition == "skip" and not durable.successful_effects:
        complete_without_delivery(claimed, durable, "skip")
        log_heartbeat(_state, f"{claimed['entry_kind']}_skip")
        return None
    if durable.disposition == "handled" and durable.successful_effects:
        complete_without_delivery(claimed, durable, "tools_only")
        log_heartbeat(_state, f"{claimed['entry_kind']}_tools_only")
        return None
    if durable.disposition != "deliver" or not (
        durable.text or durable.stickers
    ):
        record_failure(claimed, "Main returned no valid outcome")
        log_heartbeat(_state, f"{claimed['entry_kind']}_failure", "invalid")
        return None
    if not store_prepared_result(claimed, durable):
        return None
    claimed["status"] = "ready"
    claimed["result_json"] = durable.to_json()
    claimed["last_error"] = ""
    return durable


async def _deliver_autonomous(
    claimed: dict,
    durable: DurableChatResult,
) -> bool:
    if _runtime_delivery_callback is None:
        record_failure(claimed, "Runtime delivery callback is not registered")
        return False
    if claimed.get("last_error") == "delivery budget/cooldown":
        complete_without_delivery(claimed, durable, "suppressed")
        log_heartbeat(_state, f"{claimed['entry_kind']}_delivery_suppressed")
        return False
    retrying_prepared_delivery = bool(
        claimed.get("result_json") and claimed.get("last_error")
    )
    if not retrying_prepared_delivery:
        wait_seconds = delivery_wait_seconds(
            now=datetime.now(TZ),
            max_daily=int(_effective("MAX_DAILY_PROACTIVE")),
            cooldown_seconds=int(_effective("PROACTIVE_COOLDOWN_SECONDS")),
        )
        if (durable.text or durable.stickers) and wait_seconds:
            complete_without_delivery(claimed, durable, "suppressed")
            log_heartbeat(_state, f"{claimed['entry_kind']}_delivery_suppressed")
            return False
    if not begin_delivery(claimed):
        return False
    from mochi.ai_client import ChatResult

    remaining = durable
    components = []
    if remaining.text:
        components.append(("text", remaining.text))
    components.extend(("sticker", item) for item in remaining.stickers)
    for kind, value in components:
        component = (
            ChatResult(text=value)
            if kind == "text"
            else ChatResult(stickers=[value])
        )
        try:
            delivered = await _runtime_delivery_callback(
                claimed["channel_id"], component,
            )
        except Exception as exc:
            record_failure(claimed, f"transport exception: {exc}")
            log_heartbeat(
                _state, f"{claimed['entry_kind']}_delivery_failure", str(exc)[:200],
            )
            return False
        if not delivered:
            record_failure(claimed, "transport reported delivery failure")
            log_heartbeat(_state, f"{claimed['entry_kind']}_delivery_failure")
            return False
        if kind == "text":
            checkpointed = checkpoint_text_delivery(
                claimed,
                content=value,
                entry_kind=claimed["entry_kind"],
            )
        else:
            checkpointed = checkpoint_visible_delivery(claimed)
        if not checkpointed:
            return False
        remaining = remove_delivered_component(remaining, kind, value)
        if not store_delivery_progress(claimed, remaining):
            return False

    result = ChatResult.from_durable(durable)
    if durable.pending_history and not result.confirm_delivered():
        record_failure(claimed, "delivered result history was not confirmed")
        return False
    if not complete_delivery(claimed):
        return False
    log_heartbeat(
        _state, f"{claimed['entry_kind']}_delivered", durable.text[:100],
    )
    return True


async def _run_claimed_entry(claimed: dict) -> None:
    durable = await _prepare_autonomous(claimed)
    if durable is None:
        return
    await _deliver_autonomous(claimed, durable)


async def run_main_runtime_tick(
    user_id: int,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Collect facts, advance independent clocks, and run each durable claim."""
    if _state != AWAKE or _silent_pause:
        return []
    now = now or datetime.now(TZ)
    from mochi.observers import collect_attention_facts

    changed = await collect_attention_facts()
    try:
        from mochi.diary import refresh_diary_status

        refresh_diary_status(user_id)
    except Exception as exc:
        log.warning("Diary status refresh failed: %s", exc)
    ensure_schedules(
        now=now,
        attention_interval_minutes=int(_effective("ATTENTION_INTERVAL_MINUTES")),
        free_time_min_minutes=int(_effective("FREE_TIME_MIN_MINUTES")),
        free_time_max_minutes=int(_effective("FREE_TIME_MAX_MINUTES")),
    )
    if changed:
        from mochi.heartbeat_runtime import advance_attention

        advance_attention(now=now)
    created = materialize_due_runs(
        user_id=user_id,
        channel_id=user_id,
        transport=_runtime_transport,
        now=now,
        attention_interval_minutes=int(_effective("ATTENTION_INTERVAL_MINUTES")),
        free_time_min_minutes=int(_effective("FREE_TIME_MIN_MINUTES")),
        free_time_max_minutes=int(_effective("FREE_TIME_MAX_MINUTES")),
    )
    for row in get_schedulable_runs(now=now):
        claimed = claim_run(row["run_key"], now=now)
        if claimed is not None:
            await _run_claimed_entry(claimed)
    return created


async def heartbeat_loop() -> None:
    log.info(
        "Heartbeat started: interval=%dm, state=%s",
        _effective("HEARTBEAT_INTERVAL_MINUTES"),
        _state,
    )
    while True:
        interval = int(_effective("HEARTBEAT_INTERVAL_MINUTES")) * 60
        try:
            from mochi.config import OWNER_USER_ID as user_id

            if user_id is None:
                await asyncio.sleep(interval)
                continue
            now = datetime.now(TZ)
            await _run_maintenance_if_due(user_id, now)
            await _run_weekly_if_due(user_id, now)
            if _state == TRANSITIONING:
                log_heartbeat(_state, "sleep_transition")
                await asyncio.sleep(interval)
                continue
            if _state == SLEEPING:
                fallback_hour = int(_effective("FALLBACK_WAKE_HOUR"))
                if fallback_hour <= now.hour < _sleep_after_hour():
                    wake_up(f"fallback_{fallback_hour}:00")
                else:
                    log_heartbeat(_state, "sleeping")
                    await asyncio.sleep(interval)
                    continue
            sleep_action = check_silence_sleep()
            if sleep_action:
                trigger = (
                    "resleep"
                    if sleep_action["context_hint"] == "re_sleep"
                    else "silence"
                )
                await run_silent_bedtime(user_id, trigger)
                await asyncio.sleep(interval)
                continue
            _check_silence_pause()
            if _silent_pause:
                log_heartbeat(_state, "silent_pause")
                await asyncio.sleep(interval)
                continue
            await run_main_runtime_tick(user_id, now=now)
        except Exception as exc:
            log.error("Heartbeat error: %s", exc, exc_info=True)
            log_heartbeat(_state, "error", str(exc)[:200])
        await asyncio.sleep(interval)
