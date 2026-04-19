"""Reminder timer — event-driven delivery of time-based reminders.

Uses a heapq min-heap + asyncio.Event for precise, zero-polling scheduling.
Handles recurrence (daily/weekdays/weekly/monthly). Uses LLM to rephrase
reminders in Mochi's voice before delivery (falls back to raw text on failure).
"""

import asyncio
import heapq
import logging
from datetime import datetime, timedelta, timezone

from mochi.config import TZ
# TODO: reminder_timer imports from skill layer — pre-existing coupling, not ideal
from mochi.skills.reminder.queries import (
    get_all_pending_reminders,
    mark_reminder_fired,
    reschedule_reminder,
)
from mochi.db import save_message, get_core_memory, log_usage
from mochi.llm import get_client_for_tier
from mochi.prompt_loader import get_prompt

log = logging.getLogger(__name__)

_send_callback = None
_heap: list[tuple[str, int, dict]] = []  # (utc_iso, reminder_id, reminder_dict)
_heap_event: asyncio.Event | None = None
_MAX_RETRY = 3
_retry_counts: dict[int, int] = {}


def set_send_callback(callback) -> None:
    """Register the function to send reminder messages.

    Signature: async def callback(user_id: int, text: str) -> None
    """
    global _send_callback
    _send_callback = callback


def notify_new_reminder() -> None:
    """Wake the scheduler when a reminder has been created or deleted."""
    if _heap_event is not None:
        _heap_event.set()


# ── Recurrence ────────────────────────────────────────────────────────

def _compute_next_occurrence(remind_at: datetime, recurrence: str) -> datetime | None:
    """Compute next fire time for a recurring reminder. Returns None if unknown."""
    if not recurrence:
        return None

    if recurrence == "daily":
        return remind_at + timedelta(days=1)
    elif recurrence == "weekdays":
        next_dt = remind_at + timedelta(days=1)
        while next_dt.weekday() >= 5:
            next_dt += timedelta(days=1)
        return next_dt
    elif recurrence == "weekly":
        return remind_at + timedelta(weeks=1)
    elif recurrence == "monthly":
        month = remind_at.month + 1
        year = remind_at.year
        if month > 12:
            month = 1
            year += 1
        day = min(remind_at.day, 28)
        return remind_at.replace(year=year, month=month, day=day)
    elif recurrence.startswith("monthly_on:"):
        try:
            target_day = int(recurrence.split(":")[1])
            month = remind_at.month + 1
            year = remind_at.year
            if month > 12:
                month = 1
                year += 1
            day = min(target_day, 28)
            return remind_at.replace(year=year, month=month, day=day)
        except (ValueError, IndexError):
            return None

    log.warning("Unknown recurrence format: %s", recurrence)
    return None


# ── LLM rephrase ─────────────────────────────────────────────────────

async def _rephrase_reminder(message: str, user_id: int) -> str:
    """Ask LLM to rephrase reminder in Mochi's voice. Falls back to raw text."""
    fallback = f"⏰ {message}"
    try:
        soul = get_prompt("system_chat/soul") or ""
        template = get_prompt("reminder_deliver")
        if not template:
            return fallback

        system_prompt = template.replace("{soul_personality}", soul)
        core_memory = get_core_memory(user_id)
        if core_memory:
            system_prompt += f"\n\n## 你对用户的了解\n{core_memory}"

        user_msg = message

        client = get_client_for_tier("chat")
        response = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=256,
            ),
            timeout=30,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model,
            purpose="reminder_deliver",
            reasoning_tokens=response.reasoning_tokens,
            cached_prompt_tokens=response.cached_prompt_tokens,
        )

        text = (response.content or "").strip()
        return text if text else fallback

    except asyncio.TimeoutError:
        log.warning("LLM rephrase timed out for reminder, using fallback")
        return fallback
    except Exception as e:
        log.warning("LLM rephrase failed for reminder: %s", e)
        return fallback


# ── Heap helpers ──────────────────────────────────────────────────────

def _to_utc_key(raw_remind_at: str) -> str | None:
    """Parse remind_at and return UTC ISO string for heap ordering."""
    try:
        dt = datetime.fromisoformat(raw_remind_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _push_to_heap(reminder: dict) -> None:
    """Normalize remind_at and push onto the min-heap."""
    utc_key = _to_utc_key(reminder.get("remind_at", ""))
    if utc_key is None:
        log.warning(
            "Reminder #%d has invalid remind_at=%r, skipping",
            reminder.get("id"), reminder.get("remind_at"),
        )
        return
    heapq.heappush(_heap, (utc_key, reminder["id"], reminder))


def _reload_heap() -> None:
    """Re-read all pending reminders from DB and rebuild the heap."""
    global _heap
    try:
        _heap = []
        for r in get_all_pending_reminders():
            _push_to_heap(r)
    except Exception as e:
        log.error("Failed to reload reminder heap: %s", e, exc_info=True)


# ── Fire logic ────────────────────────────────────────────────────────

async def _fire_reminder(reminder: dict) -> None:
    """Rephrase and send a reminder, then handle recurrence. Runs as a task."""
    user_id = reminder["user_id"]
    message = reminder["message"]
    reminder_id = reminder["id"]
    remind_at_raw = reminder["remind_at"]

    try:
        remind_at = datetime.fromisoformat(remind_at_raw)
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=TZ)
    except (ValueError, TypeError):
        remind_at = datetime.now(TZ)

    try:
        rephrased = await _rephrase_reminder(message, user_id)
        await _send_callback(user_id, rephrased)
        save_message(user_id, "assistant", rephrased)
    except Exception as e:
        log.error("Failed to send reminder #%d: %s", reminder_id, e, exc_info=True)

    # Handle recurrence
    recurrence = reminder.get("recurrence")
    if recurrence:
        next_at = _compute_next_occurrence(remind_at, recurrence)
        if next_at:
            next_iso = next_at.isoformat()
            reschedule_reminder(reminder_id, next_iso)
            reminder_copy = dict(reminder)
            reminder_copy["remind_at"] = next_iso
            _push_to_heap(reminder_copy)
            log.info("Recurring reminder #%d rescheduled to %s",
                     reminder_id, next_iso)
        else:
            mark_reminder_fired(reminder_id)
    else:
        mark_reminder_fired(reminder_id)

    _retry_counts.pop(reminder_id, None)


# ── Main loop ─────────────────────────────────────────────────────────

async def reminder_loop() -> None:
    """Event-driven reminder scheduler using heapq + asyncio.Event."""
    global _heap_event
    _heap_event = asyncio.Event()

    log.info("Reminder timer started (event-driven)")

    # Load all pending reminders into the heap at startup
    _reload_heap()
    log.info("Loaded %d pending reminders into heap", len(_heap))

    while True:
        try:
            _heap_event.clear()

            if not _heap:
                await _heap_event.wait()
                _reload_heap()
                continue

            utc_key, _rid, reminder = _heap[0]

            try:
                fire_time = datetime.fromisoformat(utc_key)
            except (ValueError, TypeError):
                heapq.heappop(_heap)
                log.warning("Invalid UTC key in heap, discarding reminder #%d", _rid)
                mark_reminder_fired(_rid)
                continue

            now_utc = datetime.now(timezone.utc)
            delay = (fire_time - now_utc).total_seconds()

            if delay > 0:
                try:
                    await asyncio.wait_for(_heap_event.wait(), timeout=delay)
                    # Woken early — reload heap and re-evaluate
                    _reload_heap()
                    continue
                except asyncio.TimeoutError:
                    pass  # Fire time reached

            heapq.heappop(_heap)

            reminder_id = reminder["id"]
            user_id = reminder["user_id"]
            message = reminder["message"]
            log.info("Firing reminder #%d for user %d: %.50s",
                     reminder_id, user_id, message)

            if not _send_callback:
                count = _retry_counts.get(reminder_id, 0) + 1
                _retry_counts[reminder_id] = count
                if count >= _MAX_RETRY:
                    log.error(
                        "Reminder #%d undelivered after %d retries "
                        "(no send_callback) — marking fired",
                        reminder_id, count,
                    )
                    mark_reminder_fired(reminder_id)
                    _retry_counts.pop(reminder_id, None)
                else:
                    log.warning(
                        "Reminder #%d due but _send_callback is None — "
                        "retry %d/%d in 60s",
                        reminder_id, count, _MAX_RETRY,
                    )
                    _push_to_heap(reminder)
                    await asyncio.sleep(60)
                continue

            asyncio.create_task(_fire_reminder(reminder))

        except Exception as e:
            log.error("Reminder timer error: %s", e, exc_info=True)
            await asyncio.sleep(30)
