"""Reminder timer — precise delivery of time-based reminders.

Lightweight asyncio loop that fires reminders at their exact remind_at time.
Handles recurrence (daily/weekdays/weekly/monthly). Uses LLM to rephrase
reminders in Mochi's voice before delivery (falls back to raw text on failure).
"""

import asyncio
import logging
from datetime import datetime, timedelta

from mochi.config import TZ
from mochi.skills.reminder.queries import (
    get_next_pending_reminder,
    mark_reminder_fired,
    reschedule_reminder,
)
from mochi.db import save_message, get_core_memory, log_usage
from mochi.llm import get_client_for_tier
from mochi.prompt_loader import get_prompt

log = logging.getLogger(__name__)

_send_callback = None


def set_send_callback(callback) -> None:
    """Register the function to send reminder messages.

    Signature: async def callback(user_id: int, text: str) -> None
    """
    global _send_callback
    _send_callback = callback


def _compute_next_occurrence(remind_at: datetime, recurrence: str) -> datetime | None:
    """Compute next fire time for a recurring reminder. Returns None if unknown."""
    if not recurrence:
        return None

    if recurrence == "daily":
        return remind_at + timedelta(days=1)
    elif recurrence == "weekdays":
        next_dt = remind_at + timedelta(days=1)
        while next_dt.weekday() >= 5:  # skip Sat/Sun
            next_dt += timedelta(days=1)
        return next_dt
    elif recurrence == "weekly":
        return remind_at + timedelta(weeks=1)
    elif recurrence == "monthly":
        # Same day next month
        month = remind_at.month + 1
        year = remind_at.year
        if month > 12:
            month = 1
            year += 1
        day = min(remind_at.day, 28)  # safe for all months
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
                temperature=0.7,
                max_tokens=256,
            ),
            timeout=30,
        )

        log_usage(
            response.prompt_tokens, response.completion_tokens,
            response.total_tokens, model=response.model,
            purpose="reminder_deliver",
        )

        text = (response.content or "").strip()
        return text if text else fallback

    except asyncio.TimeoutError:
        log.warning("LLM rephrase timed out for reminder, using fallback")
        return fallback
    except Exception as e:
        log.warning("LLM rephrase failed for reminder: %s", e)
        return fallback


async def reminder_loop() -> None:
    """Main reminder loop. Polls for next reminder, sleeps until fire time."""
    log.info("Reminder timer started")

    while True:
        try:
            reminder = get_next_pending_reminder()
            if not reminder:
                await asyncio.sleep(60)
                continue

            # Parse remind_at
            try:
                remind_at = datetime.fromisoformat(reminder["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=TZ)
            except (ValueError, TypeError):
                log.warning("Invalid remind_at for reminder #%d, marking fired",
                            reminder["id"])
                mark_reminder_fired(reminder["id"])
                continue

            now = datetime.now(TZ)
            delay = (remind_at - now).total_seconds()

            if delay > 60:
                # Not due yet — sleep but re-check every 60s
                # (a new sooner reminder may be created while we sleep)
                await asyncio.sleep(60)
                continue

            if delay > 0:
                await asyncio.sleep(delay)

            # Fire the reminder
            user_id = reminder["user_id"]
            message = reminder["message"]
            log.info("Firing reminder #%d: %s", reminder["id"], message[:50])

            if _send_callback:
                rephrased = await _rephrase_reminder(message, user_id)
                await _send_callback(user_id, rephrased)
                save_message(user_id, "assistant", rephrased)

            # Handle recurrence
            recurrence = reminder.get("recurrence")
            if recurrence:
                next_at = _compute_next_occurrence(remind_at, recurrence)
                if next_at:
                    reschedule_reminder(reminder["id"], next_at.isoformat())
                    log.info("Recurring reminder #%d rescheduled to %s",
                             reminder["id"], next_at.isoformat())
                else:
                    mark_reminder_fired(reminder["id"])
            else:
                mark_reminder_fired(reminder["id"])

        except Exception as e:
            log.error("Reminder timer error: %s", e, exc_info=True)
            await asyncio.sleep(30)  # back off on error
