"""Event-driven, persistent delivery for notify and Self Reminders."""

from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from mochi.config import TZ
from mochi.db import (
    get_tool_executions_for_turn,
    log_usage,
    save_message_once,
)
from mochi.core_store import read_core
from mochi.llm import get_client_for_tier
from mochi.main_runtime import DurableChatResult, MainRuntimeEntry
from mochi.prompt_loader import get_prompt
from mochi.transport.utils import normalize_legacy_bubble_delimiters
from mochi.skills.reminder.queries import (
    begin_delivery,
    claim_reminder,
    complete_without_delivery,
    complete_reminder_delivery,
    get_next_active_lease_expiry,
    get_schedulable_reminders,
    record_reminder_failure,
    store_delivery_progress,
    store_prepared_result,
    store_prepared_text,
)


log = logging.getLogger(__name__)

_send_callback = None
_self_prepare_callback = None
_self_delivery_callback = None
_self_transport = ""
_heap: list[tuple[str, int, dict]] = []
_heap_event: asyncio.Event | None = None
_active_ids: set[int] = set()
_LEASE_SECONDS = 300
_SELF_MAIN_TIMEOUT_SECONDS = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def set_send_callback(callback) -> None:
    """Register ``async callback(user_id, text) -> bool``."""
    global _send_callback
    _send_callback = callback
    notify_new_reminder()


def set_self_reminder_callbacks(
    prepare_callback,
    delivery_callback,
    transport: str,
) -> None:
    """Register Main preparation and active-transport delivery."""
    global _self_prepare_callback, _self_delivery_callback, _self_transport
    _self_prepare_callback = prepare_callback
    _self_delivery_callback = delivery_callback
    _self_transport = transport
    notify_new_reminder()


def notify_new_reminder() -> None:
    if _heap_event is not None:
        _heap_event.set()


def _compute_next_occurrence(
    remind_at: datetime,
    recurrence: str,
) -> datetime | None:
    if not recurrence:
        return None
    if recurrence == "daily":
        return remind_at + timedelta(days=1)
    if recurrence == "weekdays":
        next_dt = remind_at + timedelta(days=1)
        while next_dt.weekday() >= 5:
            next_dt += timedelta(days=1)
        return next_dt
    if recurrence == "weekly":
        return remind_at + timedelta(weeks=1)
    if recurrence == "monthly":
        month = remind_at.month + 1
        year = remind_at.year
        if month > 12:
            month = 1
            year += 1
        return remind_at.replace(
            year=year, month=month, day=min(remind_at.day, 28),
        )
    if recurrence.startswith("monthly_on:"):
        try:
            target_day = int(recurrence.split(":")[1])
            month = remind_at.month + 1
            year = remind_at.year
            if month > 12:
                month = 1
                year += 1
            return remind_at.replace(
                year=year, month=month, day=min(target_day, 28),
            )
        except (ValueError, IndexError):
            return None
    return None


def _next_remind_at(reminder: dict) -> str | None:
    recurrence = reminder.get("recurrence")
    if not recurrence:
        return None
    try:
        remind_at = datetime.fromisoformat(reminder["remind_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if remind_at.tzinfo is None:
        remind_at = remind_at.replace(tzinfo=TZ)
    next_occurrence = _compute_next_occurrence(remind_at, recurrence)
    now = _utc_now()
    while next_occurrence is not None and next_occurrence.astimezone(
        timezone.utc
    ) <= now:
        later = _compute_next_occurrence(next_occurrence, recurrence)
        if later is None or later <= next_occurrence:
            return None
        next_occurrence = later
    return next_occurrence.isoformat() if next_occurrence else None


async def _rephrase_reminder(message: str, user_id: int) -> str:
    """Prepare one durable notification; raw content is a safe fallback."""
    fallback = f"⏰ {message}"
    try:
        template = get_prompt("reminder_deliver")
        if not template:
            return fallback
        core = read_core()
        agent = get_prompt("system_chat/agent")
        system_prompt = "\n\n".join(
            part for part in (core, agent, template) if part
        )
        response = await asyncio.wait_for(
            asyncio.to_thread(
                get_client_for_tier("main").chat,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message},
                ],
                max_tokens=256,
            ),
            timeout=30,
        )
        log_usage(
            response.prompt_tokens,
            response.completion_tokens,
            response.total_tokens,
            model=response.model,
            purpose="reminder_deliver",
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
        )
        return normalize_legacy_bubble_delimiters(
            response.content or "",
        ) or fallback
    except Exception as exc:
        log.warning("Reminder rewrite unavailable, using raw content: %s", exc)
        return fallback


def _to_utc_key(raw_time: str) -> str | None:
    try:
        value = datetime.fromisoformat(raw_time)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(timezone.utc).isoformat()


def _push_to_heap(reminder: dict) -> None:
    effective_at = reminder.get("effective_at") or reminder.get("remind_at", "")
    utc_key = _to_utc_key(effective_at)
    if utc_key is None:
        log.error(
            "Reminder #%s has invalid effective time %r",
            reminder.get("id"),
            effective_at,
        )
        return
    heapq.heappush(_heap, (utc_key, reminder["id"], reminder))


def _reload_heap() -> None:
    global _heap
    try:
        _heap = []
        for reminder in get_schedulable_reminders(now=_utc_now()):
            if reminder["id"] not in _active_ids:
                _push_to_heap(reminder)
    except Exception:
        log.exception("Failed to reload reminder heap")


def _finish_fire_task(reminder_id: int, task: asyncio.Task) -> None:
    _active_ids.discard(reminder_id)
    if not task.cancelled() and task.exception() is not None:
        log.error("Reminder #%d task failed unexpectedly", reminder_id)
    notify_new_reminder()


async def _persist_failure(reminder: dict, error: str) -> None:
    retry_at = record_reminder_failure(
        reminder["id"],
        reminder["claimed_at"],
        error,
        now=_utc_now(),
    )
    if retry_at is not None:
        log.warning(
            "Reminder #%d will retry at %s: %s",
            reminder["id"],
            retry_at.isoformat(),
            error,
        )


async def _prepare_self_reminder(
    claimed: dict,
) -> DurableChatResult | None:
    if _self_prepare_callback is None:
        await _persist_failure(
            claimed, "self reminder Main callback is not registered",
        )
        return None
    row_transport = (claimed.get("transport") or _self_transport).strip()
    if not row_transport or row_transport != _self_transport:
        await _persist_failure(
            claimed,
            f"self reminder belongs to inactive transport {row_transport or '?'}",
        )
        return None
    try:
        entry = MainRuntimeEntry.self_reminder(
            reminder_id=claimed["id"],
            scheduled_for=claimed["remind_at"],
            intent=claimed.get("context") or "",
            user_id=claimed["user_id"],
            channel_id=claimed["channel_id"],
            transport=row_transport,
            claim_token=claimed["claimed_at"],
            lease_until=claimed["lease_until"],
            recurrence=claimed.get("recurrence"),
        )
        result = await asyncio.wait_for(
            _self_prepare_callback(entry),
            timeout=_SELF_MAIN_TIMEOUT_SECONDS,
        )
        durable = result.to_durable()
    except Exception as exc:
        await _persist_failure(claimed, f"Main failed: {exc}")
        return None

    if durable.disposition == "skip" and not durable.successful_effects:
        complete_without_delivery(
            claimed["id"],
            claimed["claimed_at"],
            durable.to_json(),
            "no_op",
            handled_at=_utc_now(),
            next_remind_at=_next_remind_at(claimed),
        )
        return None
    if durable.disposition == "handled" and durable.successful_effects:
        complete_without_delivery(
            claimed["id"],
            claimed["claimed_at"],
            durable.to_json(),
            "handled",
            handled_at=_utc_now(),
            next_remind_at=_next_remind_at(claimed),
        )
        return None
    if durable.disposition != "deliver" or not (
        durable.text or durable.stickers
    ):
        await _persist_failure(
            claimed, "Main returned no deliverable or handled outcome",
        )
        return None
    durable_json = durable.to_json()
    if not store_prepared_result(
        claimed["id"],
        claimed["claimed_at"],
        durable_json,
    ):
        return None
    claimed["status"] = "ready"
    claimed["result_json"] = durable_json
    return durable


def _recover_prior_tool_attempt(
    claimed: dict,
) -> DurableChatResult | None:
    """Treat an interrupted stable turn conservatively instead of replaying tools."""
    turn_id = (
        f"self-reminder:{claimed['id']}:{claimed['remind_at']}"
    )
    executions = get_tool_executions_for_turn(turn_id)
    if not executions:
        return None
    log.warning(
        "Self reminder #%d has prior tool ledger; finishing without replay",
        claimed["id"],
    )
    return DurableChatResult(
        tool_audit=tuple({
            "name": item["tool_name"],
            "status": item["status"],
            "state_changed": bool(item["state_changed"]),
        } for item in executions),
        successful_effects=any(
            item["status"] == "success" and item["state_changed"]
            for item in executions
        ),
        disposition="handled",
    )


async def _validate_self_transport(claimed: dict) -> bool:
    row_transport = (claimed.get("transport") or _self_transport).strip()
    if row_transport and row_transport == _self_transport:
        return True
    await _persist_failure(
        claimed,
        f"self reminder belongs to inactive transport {row_transport or '?'}",
    )
    return False


async def _deliver_self_reminder(
    claimed: dict,
    durable: DurableChatResult,
) -> bool:
    if _self_delivery_callback is None:
        await _persist_failure(
            claimed, "self reminder delivery callback is not registered",
        )
        return False
    from mochi.ai_client import ChatResult

    remaining = durable
    components = []
    if remaining.text:
        components.append(("text", remaining.text))
    components.extend(("sticker", item) for item in remaining.stickers)
    for component_kind, value in components:
        component = (
            ChatResult(text=value)
            if component_kind == "text"
            else ChatResult(stickers=[value])
        )
        try:
            delivered = await _self_delivery_callback(
                claimed["channel_id"], component,
            )
        except Exception as exc:
            await _persist_failure(
                claimed, f"transport exception: {exc}",
            )
            return False
        if not delivered:
            await _persist_failure(
                claimed, "transport reported delivery failure",
            )
            return False
        if component_kind == "text":
            remaining = replace(remaining, text="")
        else:
            stickers = list(remaining.stickers)
            stickers.remove(value)
            remaining = replace(remaining, stickers=tuple(stickers))
        if not store_delivery_progress(
            claimed["id"],
            claimed["claimed_at"],
            remaining.to_json(),
        ):
            return False

    result = ChatResult.from_durable(durable)
    try:
        if not result.confirm_delivered():
            raise RuntimeError("prepared result has no confirmable history")
    except Exception as exc:
        await _persist_failure(
            claimed, f"history confirmation failed after send: {exc}",
        )
        return False
    return True


async def _fire_reminder(reminder: dict) -> None:
    claimed = claim_reminder(
        reminder["id"],
        now=_utc_now(),
        lease_seconds=_LEASE_SECONDS,
    )
    if claimed is None:
        return
    kind = claimed.get("kind", "notify")
    if kind == "notify" and _send_callback is None:
        await _persist_failure(
            claimed, "reminder send callback is not registered",
        )
        return

    prepared_text = claimed.get("prepared_text")
    durable_result = None
    if kind == "self":
        result_json = claimed.get("result_json")
        if result_json:
            try:
                durable_result = DurableChatResult.from_json(result_json)
            except (TypeError, ValueError) as exc:
                await _persist_failure(
                    claimed, f"stored result is invalid: {exc}",
                )
                return
        else:
            recovered = _recover_prior_tool_attempt(claimed)
            if recovered is not None:
                complete_without_delivery(
                    claimed["id"],
                    claimed["claimed_at"],
                    recovered.to_json(),
                    "handled",
                    handled_at=_utc_now(),
                    next_remind_at=_next_remind_at(claimed),
                )
                return
            durable_result = await _prepare_self_reminder(claimed)
            if durable_result is None:
                return
    elif not prepared_text:
        prepared_text = await _rephrase_reminder(
            claimed["message"], claimed["user_id"],
        )
        if not store_prepared_text(
            claimed["id"], claimed["claimed_at"], prepared_text,
        ):
            return
        claimed["status"] = "ready"
        claimed["prepared_text"] = prepared_text

    if kind == "self" and not await _validate_self_transport(claimed):
        return
    cursor = begin_delivery(claimed["id"], claimed["claimed_at"])
    if cursor is None:
        return

    if kind == "self":
        if not await _deliver_self_reminder(claimed, durable_result):
            return
    else:
        try:
            delivered = await _send_callback(
                claimed["user_id"], prepared_text,
            )
        except Exception as exc:
            await _persist_failure(
                claimed, f"transport exception: {exc}",
            )
            return
        if not delivered:
            await _persist_failure(
                claimed, "transport reported delivery failure",
            )
            return

    try:
        if kind == "notify":
            save_message_once(
                claimed["user_id"],
                "assistant",
                prepared_text,
                turn_id=(
                    f"notify-reminder:{claimed['id']}:"
                    f"{claimed['remind_at']}"
                ),
                processed=True,
            )
        completed = complete_reminder_delivery(
            claimed["id"],
            claimed["claimed_at"],
            delivered_at=_utc_now(),
            next_remind_at=_next_remind_at(claimed),
        )
    except Exception as exc:
        await _persist_failure(
            claimed, f"finalization failed after send: {exc}",
        )
        return
    if completed:
        log.info(
            "Reminder #%d delivered at cursor %d",
            claimed["id"], cursor,
        )


async def reminder_loop() -> None:
    """Run the scheduler; SQLite remains the delivery authority."""
    global _heap_event
    _heap_event = asyncio.Event()
    _reload_heap()
    while True:
        try:
            if not _heap:
                _heap_event.clear()
                _reload_heap()
                if _heap:
                    continue
                lease_expiry = get_next_active_lease_expiry(now=_utc_now())
                if lease_expiry is None:
                    await _heap_event.wait()
                else:
                    delay = max(
                        0, (lease_expiry - _utc_now()).total_seconds(),
                    )
                    try:
                        await asyncio.wait_for(
                            _heap_event.wait(), timeout=delay,
                        )
                    except asyncio.TimeoutError:
                        pass
                _reload_heap()
                continue

            utc_key, reminder_id, reminder = _heap[0]
            fire_time = datetime.fromisoformat(utc_key)
            delay = (fire_time - _utc_now()).total_seconds()
            if delay > 0:
                _heap_event.clear()
                try:
                    await asyncio.wait_for(
                        _heap_event.wait(), timeout=delay,
                    )
                    _reload_heap()
                    continue
                except asyncio.TimeoutError:
                    pass
            heapq.heappop(_heap)
            if reminder_id in _active_ids:
                continue
            _active_ids.add(reminder_id)
            task = asyncio.create_task(_fire_reminder(reminder))
            task.add_done_callback(
                lambda finished, rid=reminder_id: _finish_fire_task(
                    rid, finished,
                )
            )
        except Exception:
            log.exception("Reminder timer error")
            await asyncio.sleep(30)
