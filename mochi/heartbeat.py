"""Heartbeat — Observe → Think → Act autonomous loop.

The heartbeat is what makes MochiBot a companion, not just a chatbot.
It runs in the background, perceiving the world and deciding when to
proactively reach out.

Architecture:
  - Observe (every N minutes): collect state — time, silence duration, etc.
  - Think (on delta or fallback): LLM decides what to do
  - Act: execute the decision (send message / save memory / nothing)

The Think step only fires when something changed (delta detection) or
after a fallback timeout, saving LLM tokens on quiet periods.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from mochi.config import (
    HEARTBEAT_INTERVAL_MINUTES,
    AWAKE_HOUR_START, AWAKE_HOUR_END,
    FORCE_SLEEP_HOUR, FORCE_WAKE_HOUR,
    MAX_DAILY_PROACTIVE, PROACTIVE_COOLDOWN_SECONDS,
    THINK_FALLBACK_MINUTES,
    MORNING_REPORT_HOUR, EVENING_REPORT_HOUR,
    TIMEZONE_OFFSET_HOURS,
    OWNER_USER_ID,
)
from mochi.llm import get_client
from mochi.prompt_loader import get_full_prompt
from mochi.db import (
    log_heartbeat,
    get_last_heartbeat_log,
    get_core_memory,
    get_last_user_message_time,
    get_message_count_today,
    get_upcoming_reminders,
    get_active_todo_count,
    save_message,
    log_usage,
)
from mochi.runtime_state import (
    get_maintenance_summary,
    clear_maintenance_summary,
    get_user_status,
)

log = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=TIMEZONE_OFFSET_HOURS))

# ═══════════════════════════════════════════════════════════════════════════
# State Machine
# ═══════════════════════════════════════════════════════════════════════════

SLEEPING = "SLEEPING"
AWAKE = "AWAKE"


def _init_state() -> str:
    """Determine initial state based on current hour."""
    hour = datetime.now(TZ).hour
    if AWAKE_HOUR_START <= hour < AWAKE_HOUR_END:
        return AWAKE
    return SLEEPING


_state: str = _init_state()
_state_changed_at: datetime = datetime.now(TZ)
_last_think_at: datetime | None = None
_last_proactive_at: datetime | None = None
_proactive_count_today: int = 0
_last_proactive_date: str = ""
_last_morning_report_date: str = ""
_last_evening_report_date: str = ""

# Send callback — set by transport layer
_send_callback = None


def set_send_callback(callback) -> None:
    """Register the function to send proactive messages.

    Called by the transport layer during setup.
    Signature: async def callback(user_id: int, text: str) -> None
    """
    global _send_callback
    _send_callback = callback
    log.info("Heartbeat send callback registered")


# ═══════════════════════════════════════════════════════════════════════════
# Observe — collect world state (zero LLM calls)
# ═══════════════════════════════════════════════════════════════════════════

async def _observe(user_id: int) -> dict:
    """Collect current world state. Pure data, no judgment."""
    now = datetime.now(TZ)

    # Time context
    observation = {
        "timestamp": now.isoformat(),
        "hour": now.hour,
        "weekday": now.strftime("%A"),
        "state": _state,
    }

    # Time-of-day label (helps LLM reason about context)
    if 5 <= now.hour < 9:
        observation["time_of_day"] = "early_morning"
    elif 9 <= now.hour < 12:
        observation["time_of_day"] = "morning"
    elif 12 <= now.hour < 14:
        observation["time_of_day"] = "lunch"
    elif 14 <= now.hour < 18:
        observation["time_of_day"] = "afternoon"
    elif 18 <= now.hour < 21:
        observation["time_of_day"] = "evening"
    else:
        observation["time_of_day"] = "night"

    # Silence duration
    last_msg_time = get_last_user_message_time(user_id)
    if last_msg_time:
        try:
            last_dt = datetime.fromisoformat(last_msg_time)
            silence_hours = (now - last_dt).total_seconds() / 3600
            observation["silence_hours"] = round(silence_hours, 1)
        except (ValueError, TypeError):
            observation["silence_hours"] = None
    else:
        observation["silence_hours"] = None

    # Conversation activity today
    msg_count = get_message_count_today(user_id)
    observation["messages_today"] = msg_count

    # Active todos
    todo_count = get_active_todo_count(user_id)
    if todo_count > 0:
        observation["active_todos"] = todo_count

    # Upcoming reminders (within 2 hours)
    upcoming = get_upcoming_reminders(user_id, hours_ahead=2)
    if upcoming:
        observation["upcoming_reminders"] = [
            {"message": r["message"], "remind_at": r["remind_at"]}
            for r in upcoming
        ]

    # Core memory snapshot
    core = get_core_memory(user_id)
    if core:
        observation["core_memory_preview"] = core[:200]

    # User status
    observation["user_status"] = get_user_status()

    # Maintenance summary (if available)
    maint = get_maintenance_summary()
    if maint:
        observation["maintenance_summary"] = maint

    # Observer plugin data (weather, habits, etc.)
    try:
        from mochi.observers import collect_all
        observer_data = await collect_all()
        if observer_data:
            observation["observers"] = observer_data
    except Exception as e:
        log.warning("Observer collect_all failed: %s", e)

    return observation


# ═══════════════════════════════════════════════════════════════════════════
# Daily Reports — morning briefing & evening reflection
# ═══════════════════════════════════════════════════════════════════════════

def _report_due() -> str | None:
    """Return 'morning' or 'evening' if a scheduled report hasn't been sent today."""
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    hour = now.hour
    if MORNING_REPORT_HOUR >= 0 and hour == MORNING_REPORT_HOUR and today != _last_morning_report_date:
        return "morning"
    if EVENING_REPORT_HOUR >= 0 and hour == EVENING_REPORT_HOUR and today != _last_evening_report_date:
        return "evening"
    return None


async def _send_report(report_type: str, user_id: int, observation: dict) -> None:
    """Generate and send a daily report using the appropriate prompt."""
    global _last_morning_report_date, _last_evening_report_date

    prompt = get_full_prompt(f"report_{report_type}", "Report")
    if not prompt:
        log.warning("Report prompt not found: report_%s", report_type)
        return

    obs_text = json.dumps(observation, ensure_ascii=False, indent=2)
    client = get_client(purpose="think")
    response = client.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Current context:\n{obs_text}"},
        ],
        temperature=0.7,
        max_tokens=600,
    )

    log_usage(
        response.prompt_tokens, response.completion_tokens,
        response.total_tokens, model=response.model, purpose=f"report_{report_type}",
    )

    content = response.content.strip()
    if content and _send_callback:
        await _send_callback(user_id, content)
        save_message(user_id, "assistant", content)
        log_heartbeat(_state, f"report_{report_type}", content[:100])
        log.info("Daily %s report sent", report_type)
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        if report_type == "morning":
            _last_morning_report_date = today
        else:
            _last_evening_report_date = today
    else:
        log.warning("Report skipped: no callback or empty content")


# ═══════════════════════════════════════════════════════════════════════════
# Think — LLM decides what to do (only on delta or fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _should_think(observation: dict) -> bool:
    """Decide whether to invoke LLM Think step."""
    global _last_think_at

    now = datetime.now(TZ)

    # Always think on first run
    if _last_think_at is None:
        return True

    minutes_since = (now - _last_think_at).total_seconds() / 60

    # Fallback: think at least every N minutes
    if minutes_since >= THINK_FALLBACK_MINUTES:
        return True

    # Delta: maintenance summary arrived
    if observation.get("maintenance_summary"):
        return True

    # Delta: upcoming reminders need attention
    if observation.get("upcoming_reminders"):
        return True

    # Delta: activity_pattern observer detected anomalous signals
    # e.g. "silent_after_active_day", "unusually_quiet", "silent_3_days"
    pattern_signals = (
        observation.get("observers", {})
        .get("activity_pattern", {})
        .get("signals", [])
    )
    if pattern_signals:
        return True

    return False


async def _think(observation: dict, user_id: int) -> dict | None:
    """Ask LLM to decide what to do based on observation.

    Returns action dict or None.
    Expected actions: {"type": "notify", "content": "..."} or {"type": "nothing"}
    """
    global _last_think_at
    _last_think_at = datetime.now(TZ)

    prompt = get_full_prompt("think_system", "Think")
    if not prompt:
        log.warning("think_system prompt not found")
        return None

    obs_text = json.dumps(observation, ensure_ascii=False, indent=2)

    client = get_client(purpose="think")
    response = client.chat(
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Current observation:\n{obs_text}"},
        ],
        temperature=0.5,
        max_tokens=512,
    )

    log_usage(
        response.prompt_tokens, response.completion_tokens,
        response.total_tokens, model=response.model, purpose="heartbeat_think",
    )

    # Parse JSON action
    try:
        action = json.loads(response.content)
        return action if isinstance(action, dict) else None
    except json.JSONDecodeError:
        # Try to extract JSON from mixed content
        content = response.content
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end])
            except json.JSONDecodeError:
                pass
        log.warning("Think response was not valid JSON")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Act — execute the Think decision
# ═══════════════════════════════════════════════════════════════════════════

async def _act(action: dict, user_id: int) -> None:
    """Execute the action decided by Think."""
    global _last_proactive_at, _proactive_count_today, _last_proactive_date

    action_type = action.get("type", "nothing")

    if action_type == "nothing":
        log_heartbeat(_state, "nothing")
        return

    if action_type == "notify":
        # Rate limiting
        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if today != _last_proactive_date:
            _proactive_count_today = 0
            _last_proactive_date = today

        if _proactive_count_today >= MAX_DAILY_PROACTIVE:
            log.info("Daily proactive limit reached (%d)", MAX_DAILY_PROACTIVE)
            log_heartbeat(_state, "rate_limited")
            return

        if _last_proactive_at:
            elapsed = (now - _last_proactive_at).total_seconds()
            if elapsed < PROACTIVE_COOLDOWN_SECONDS:
                log.info("Proactive cooldown active (%ds remaining)",
                         PROACTIVE_COOLDOWN_SECONDS - elapsed)
                log_heartbeat(_state, "cooldown")
                return

        content = action.get("content", "")
        if content and _send_callback:
            await _send_callback(user_id, content)
            _last_proactive_at = now
            _proactive_count_today += 1
            save_message(user_id, "assistant", content)
            log_heartbeat(_state, "notify", content[:100])
            log.info("Proactive message sent (%d/%d today)",
                     _proactive_count_today, MAX_DAILY_PROACTIVE)

            # Clear maintenance summary after delivering
            if "maintenance" in content.lower():
                clear_maintenance_summary()
        else:
            log_heartbeat(_state, "notify_skipped", "no callback or empty content")

    elif action_type == "save_memory":
        # Heartbeat can save observations as memory
        from mochi.db import save_memory_item
        mem_content = action.get("content", "")
        if mem_content:
            save_memory_item(user_id, category="observation", content=mem_content)
            log_heartbeat(_state, "save_memory", mem_content[:100])

    else:
        log.warning("Unknown action type: %s", action_type)
        log_heartbeat(_state, "unknown", str(action))


# ═══════════════════════════════════════════════════════════════════════════
# State Transitions
# ═══════════════════════════════════════════════════════════════════════════

def _update_state() -> None:
    """Update SLEEPING/AWAKE state based on current hour."""
    global _state, _state_changed_at

    hour = datetime.now(TZ).hour
    new_state = _state

    if _state == SLEEPING:
        if hour == FORCE_WAKE_HOUR or (AWAKE_HOUR_START <= hour < AWAKE_HOUR_END):
            new_state = AWAKE
    elif _state == AWAKE:
        if hour == FORCE_SLEEP_HOUR or hour < AWAKE_HOUR_START:
            new_state = SLEEPING

    if new_state != _state:
        log.info("State transition: %s → %s", _state, new_state)
        _state = new_state
        _state_changed_at = datetime.now(TZ)


# ═══════════════════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════════════════

async def heartbeat_loop() -> None:
    """Main heartbeat loop. Run as asyncio task."""
    interval = HEARTBEAT_INTERVAL_MINUTES * 60

    log.info("Heartbeat started: interval=%dm, awake=%d-%d, state=%s",
             HEARTBEAT_INTERVAL_MINUTES, AWAKE_HOUR_START, AWAKE_HOUR_END, _state)

    while True:
        try:
            # Re-read OWNER_USER_ID each cycle (may be auto-detected later)
            from mochi.config import OWNER_USER_ID as user_id
            if not user_id:
                log.debug("No owner set yet, heartbeat paused")
                await asyncio.sleep(interval)
                continue

            _update_state()

            if _state == SLEEPING:
                log_heartbeat(_state, "sleeping")
                await asyncio.sleep(interval)
                continue

            # Observe (cheap: no LLM — needed for both reports and think)
            observation = await _observe(user_id)

            # Scheduled daily reports take priority
            report_type = _report_due()
            if report_type:
                await _send_report(report_type, user_id, observation)

            # Think (only if delta or fallback)
            if _should_think(observation):
                action = await _think(observation, user_id)
                if action:
                    await _act(action, user_id)
                else:
                    log_heartbeat(_state, "think_no_action")
            else:
                log_heartbeat(_state, "observe_only")

        except Exception as e:
            log.error("Heartbeat error: %s", e, exc_info=True)
            log_heartbeat(_state, "error", str(e)[:200])

        await asyncio.sleep(interval)


def force_wake() -> None:
    """Force wake from SLEEPING — call when user activity is detected."""
    global _state, _state_changed_at
    if _state == SLEEPING:
        log.info("Forced wake: user activity detected")
        _state = AWAKE
        _state_changed_at = datetime.now(TZ)


def get_state() -> str:
    """Get current heartbeat state."""
    return _state


def get_stats() -> dict:
    """Get heartbeat statistics."""
    return {
        "state": _state,
        "state_changed_at": _state_changed_at.isoformat(),
        "last_think_at": _last_think_at.isoformat() if _last_think_at else None,
        "proactive_today": _proactive_count_today,
        "proactive_limit": MAX_DAILY_PROACTIVE,
    }
