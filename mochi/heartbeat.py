"""Heartbeat — Observe → Think → Act autonomous loop.

The heartbeat is what makes MochiBot a companion, not just a chatbot.
It runs in the background, perceiving the world and deciding when to
proactively reach out.

Architecture:
  - Observe (every N minutes): collect state — time, silence duration, etc.
  - Delta detection: per-observer has_delta() -- only Think when something changed
  - Think (on delta or fallback): LLM decides what to do
  - Act: execute the decision (send message / save memory / nothing)

The Think step only fires when an observer reports a delta or after a
fallback timeout, saving LLM tokens on quiet periods.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from mochi.config import (
    HEARTBEAT_INTERVAL_MINUTES,
    AWAKE_HOUR_START, AWAKE_HOUR_END,
    SLEEP_KEYWORD_HOUR_START, SLEEP_KEYWORD_HOUR_END, SLEEP_KEYWORDS,
    SILENCE_SLEEP_AFTER_HOUR, SILENCE_SLEEP_THRESHOLD_HOURS,
    SILENCE_PAUSE_DAYS, FALLBACK_WAKE_HOUR,
    MAX_DAILY_PROACTIVE, PROACTIVE_COOLDOWN_SECONDS,
    THINK_FALLBACK_MINUTES, LLM_HEARTBEAT_TIMEOUT_SECONDS,
    MAINTENANCE_HOUR, MAINTENANCE_ENABLED,
    TZ,
    OWNER_USER_ID,
)
from mochi.llm import get_client
from mochi.prompt_loader import get_prompt
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


# ═══════════════════════════════════════════════════════════════════════════
# State Persistence
# ═══════════════════════════════════════════════════════════════════════════

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".heartbeat_state"


def _persist_state(state: str, changed_at: datetime | None = None) -> None:
    """Write heartbeat state to disk so it survives restarts."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ts = (changed_at or datetime.now(TZ)).isoformat()
        _STATE_FILE.write_text(json.dumps({"state": state, "at": ts}))
    except Exception as exc:
        log.debug("Failed to persist heartbeat state: %s", exc)


# ── Runtime config override support (admin portal) ────────────────────────

_system_overrides_cache: dict[str, str] = {}
_system_overrides_cache_time: float = 0.0


def _effective(key: str):
    """Get effective config value: DB override (skill_config._system) > module import.

    Cached for 60s to avoid hitting the DB every heartbeat access.
    """
    global _system_overrides_cache, _system_overrides_cache_time
    import time as _time
    now = _time.monotonic()
    if now - _system_overrides_cache_time > 60:
        try:
            from mochi.admin.admin_db import get_system_overrides
            _system_overrides_cache = get_system_overrides()
        except Exception:
            _system_overrides_cache = {}
        _system_overrides_cache_time = now

    if key in _system_overrides_cache:
        raw = _system_overrides_cache[key]
        original = globals().get(key)
        if isinstance(original, bool):
            return raw.lower() in ("true", "1", "yes")
        if isinstance(original, int):
            try:
                return int(raw)
            except (ValueError, TypeError):
                return original
        return raw
    return globals().get(key)


async def _llm_with_timeout(coro, label: str):
    """Run a coroutine with a timeout guard. Returns None on timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=_effective('LLM_HEARTBEAT_TIMEOUT_SECONDS'))
    except asyncio.TimeoutError:
        log.error("Heartbeat LLM timeout in %s after %ds",
                  label, _effective('LLM_HEARTBEAT_TIMEOUT_SECONDS'))
        log_heartbeat(_state, f"{label}_timeout")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# State Machine: signal-driven SLEEPING / AWAKE
# ═══════════════════════════════════════════════════════════════════════════

SLEEPING = "SLEEPING"
AWAKE = "AWAKE"

# Re-sleep detection: if user woke within this window and goes silent again,
# it's a "fell asleep again" scenario (different goodnight message context).
RESLEEP_WINDOW_HOURS = 6


def _init_state() -> str:
    """Determine initial state. Prefer persisted state, fall back to hour heuristic."""
    now = datetime.now(TZ)
    # Try persisted state
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text())
            saved = data.get("state")
            saved_at = datetime.fromisoformat(data["at"])
            if saved_at.tzinfo is None:
                saved_at = saved_at.replace(tzinfo=TZ)
            age_h = (now - saved_at).total_seconds() / 3600
            if age_h < 12 and saved in (SLEEPING, AWAKE):
                log.info("Restored state %s from disk (%.1fh ago)", saved, age_h)
                return saved
    except Exception as exc:
        log.debug("Failed to read persisted heartbeat state: %s", exc)
    # Fallback: hour heuristic
    hour = now.hour
    if AWAKE_HOUR_START <= hour < AWAKE_HOUR_END:
        return AWAKE
    return SLEEPING


_state: str = _init_state()
_state_changed_at: datetime = datetime.now(TZ)
_last_think_at: datetime | None = None
_last_proactive_at: datetime | None = None
_proactive_count_today: int = 0
_last_proactive_date: str = ""
_last_maintenance_date: str = ""

# Sleep/wake tracking
_wake_reason: str | None = None
_morning_hold: bool = False
_last_sleep_at: datetime | None = None

# Silent pause: user hasn't replied in SILENCE_PAUSE_DAYS days.
# Heartbeat keeps running but no proactive messages are sent.
_silent_pause: bool = False

# Per-observer delta tracking
_prev_observer_raw: dict[str, dict] = {}

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


# ── State transition functions ────────────────────────────────────────────

def wake_up(reason: str = "unknown") -> None:
    """Transition SLEEPING → AWAKE.

    Args:
        reason: What triggered the wake — "user_message", "fallback", or
                any custom reason (e.g. "oura_sleep_end" from Oura skill).
    """
    global _state, _state_changed_at, _wake_reason, _morning_hold
    global _prev_observer_raw, _last_think_at
    if _state == SLEEPING:
        _state = AWAKE
        _state_changed_at = datetime.now(TZ)
        _wake_reason = reason
        _prev_observer_raw = {}
        _last_think_at = None
        # Morning hold: suppress proactive until user sends first message.
        # Only for non-user wakes (fallback, Oura, etc.)
        _morning_hold = reason != "user_message"
        _persist_state(AWAKE, _state_changed_at)
        log.info("WAKE UP — reason: %s, morning_hold: %s", reason, _morning_hold)


def go_to_sleep(reason: str = "unknown") -> None:
    """Transition AWAKE → SLEEPING."""
    global _state, _state_changed_at, _wake_reason, _morning_hold
    global _prev_observer_raw, _last_sleep_at
    if _state == AWAKE:
        _state = SLEEPING
        _state_changed_at = datetime.now(TZ)
        _last_sleep_at = _state_changed_at
        _wake_reason = None
        _morning_hold = False
        _prev_observer_raw = {}
        _persist_state(SLEEPING, _state_changed_at)
        log.info("SLEEPING — reason: %s", reason)


def force_wake() -> None:
    """Backward-compatible wake — delegates to wake_up("user_message")."""
    wake_up("user_message")


def clear_morning_hold() -> None:
    """Release morning hold — called when user sends their first message."""
    global _morning_hold
    if _morning_hold:
        _morning_hold = False
        log.info("Morning hold cleared — proactive messages resumed")


def check_sleep_entry(last_user_msg_text: str | None = None) -> None:
    """Check if user is going to sleep via goodnight keywords.

    Called by transport AFTER the Chat model has already replied.
    Only triggers during night hours to avoid false positives
    (e.g. "昨天好困" during daytime).
    """
    if _state != AWAKE or not last_user_msg_text:
        return

    now = datetime.now(TZ)
    hour = now.hour
    # Night window: SLEEP_KEYWORD_HOUR_START (21) to SLEEP_KEYWORD_HOUR_END (4)
    is_night = hour >= SLEEP_KEYWORD_HOUR_START or hour < SLEEP_KEYWORD_HOUR_END
    if not is_night:
        return

    text_lower = last_user_msg_text.lower().strip()
    if any(kw in text_lower for kw in SLEEP_KEYWORDS):
        go_to_sleep(reason=f"keyword: {text_lower[:20]}")


def check_silence_sleep() -> dict | None:
    """Check if user fell asleep based on silence duration.

    Returns a context dict for the heartbeat loop to generate a goodnight
    message, or None if conditions aren't met.

    The caller sends the message via send_callback, THEN calls go_to_sleep().
    """
    if _state != AWAKE:
        return None

    now = datetime.now(TZ)
    hour = now.hour

    # Only during night hours: >= SILENCE_SLEEP_AFTER_HOUR or < SLEEP_KEYWORD_HOUR_END
    is_night = hour >= SILENCE_SLEEP_AFTER_HOUR or hour < SLEEP_KEYWORD_HOUR_END
    if not is_night:
        return None

    # Check silence duration
    from mochi.config import OWNER_USER_ID as user_id
    if not user_id:
        return None
    last_msg_time = get_last_user_message_time(user_id)
    if not last_msg_time:
        return None

    try:
        last_dt = datetime.fromisoformat(last_msg_time)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=TZ)
        silence_hours = (now - last_dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return None

    if silence_hours < SILENCE_SLEEP_THRESHOLD_HOURS:
        return None

    # Distinguish first sleep vs re-sleep (woke mid-night and went silent again)
    is_resleep = (
        _last_sleep_at is not None
        and (now - _last_sleep_at).total_seconds() < RESLEEP_WINDOW_HOURS * 3600
    )
    context_hint = "re_sleep" if is_resleep else "first_sleep"

    log.info("Silence sleep detected: %.1fh silence, context=%s",
             silence_hours, context_hint)

    return {
        "context_hint": context_hint,
        "silence_hours": round(silence_hours, 1),
    }


# ── Silent pause: user absent for days ────────────────────────────────────

def is_silent_pause() -> bool:
    """Return True if bot is in silent pause mode (user absent for days)."""
    return _silent_pause


def enter_silent_pause() -> None:
    """Enter silent pause: user has been silent for SILENCE_PAUSE_DAYS days."""
    global _silent_pause
    if not _silent_pause:
        _silent_pause = True
        log.info("SILENT PAUSE — user silent for %.1f+ days, pausing proactive",
                 SILENCE_PAUSE_DAYS)


def clear_silent_pause() -> None:
    """Exit silent pause: user just sent a message."""
    global _silent_pause
    if _silent_pause:
        _silent_pause = False
        log.info("SILENT PAUSE cleared — user returned")


def _check_silence_pause() -> None:
    """Check if we should enter/exit silent pause based on last message time."""
    from mochi.config import OWNER_USER_ID as user_id
    if not user_id:
        return
    last_msg_iso = get_last_user_message_time(user_id)
    if not last_msg_iso:
        return  # no messages at all — fresh install, don't suppress

    try:
        last_dt = datetime.fromisoformat(last_msg_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=TZ)
        now = datetime.now(TZ)
        silence_hours = (now - last_dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return

    threshold_hours = SILENCE_PAUSE_DAYS * 24
    if silence_hours >= threshold_hours:
        enter_silent_pause()
    elif _silent_pause:
        clear_silent_pause()


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
        "proactive_limit": _effective('MAX_DAILY_PROACTIVE'),
        "wake_reason": _wake_reason,
        "morning_hold": _morning_hold,
    }


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

    # Diary: refresh status panel from DB then inject into observation
    try:
        from mochi.diary import refresh_diary_status, diary
        refresh_diary_status(user_id)
        observation["diary_status"] = diary.read(section="今日状態")
        observation["diary_journal"] = diary.read(section="今日日記")
    except Exception as e:
        log.warning("Diary refresh failed: %s", e)

    # First tick of the day (for Think morning awareness)
    from mochi.db import get_awake_tick_count_today
    observation["is_first_tick_today"] = get_awake_tick_count_today() == 0

    return observation


# ═══════════════════════════════════════════════════════════════════════════
# Delta Detection — per-observer change tracking
# ═══════════════════════════════════════════════════════════════════════════

def _check_observer_deltas(observation: dict) -> bool:
    """Check if any observer reports meaningful change.

    Compares current observer data against previous run using each
    observer's has_delta() method.
    """
    global _prev_observer_raw

    observer_data = observation.get("observers", {})
    if not observer_data and not _prev_observer_raw:
        return False

    has_any_delta = False

    try:
        from mochi.observers import get_all_observers
        all_observers = get_all_observers()
    except Exception:
        # Fallback: simple dict comparison
        has_any_delta = observer_data != _prev_observer_raw
        _prev_observer_raw = dict(observer_data)
        return has_any_delta

    for name, curr_data in observer_data.items():
        prev_data = _prev_observer_raw.get(name, {})
        obs = all_observers.get(name)
        if obs:
            if obs.has_delta(prev_data, curr_data):
                log.debug("Delta detected from observer: %s", name)
                has_any_delta = True
        elif prev_data != curr_data:
            has_any_delta = True

    _prev_observer_raw = dict(observer_data)
    return has_any_delta


# ═══════════════════════════════════════════════════════════════════════════
# Nightly Maintenance — trigger at MAINTENANCE_HOUR
# ═══════════════════════════════════════════════════════════════════════════

async def _run_maintenance_if_due(user_id: int) -> bool:
    """Run nightly maintenance if MAINTENANCE_HOUR and not yet run today."""
    global _last_maintenance_date

    if not _effective('MAINTENANCE_ENABLED'):
        return False

    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour != _effective('MAINTENANCE_HOUR') or today == _last_maintenance_date:
        return False

    _last_maintenance_date = today
    log.info("Running nightly maintenance...")

    try:
        from mochi.skills.maintenance.handler import run_maintenance
        results = await run_maintenance(user_id)
        log_heartbeat(_state, "maintenance", str(results)[:200])
    except Exception as e:
        log.error("Nightly maintenance failed: %s", e, exc_info=True)
        log_heartbeat(_state, "maintenance_error", str(e)[:200])

    # Archive diary and clear for new day
    try:
        from mochi.diary import diary
        raw = diary.read_raw()
        if raw:
            diary.snapshot(raw)
            diary.clear()
    except Exception as e:
        log.warning("Diary archive failed: %s", e)

    return True


# ═══════════════════════════════════════════════════════════════════════════
# Think — LLM decides what to do (only on delta or fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _should_think(observation: dict) -> bool:
    """Decide whether to invoke LLM Think step.

    Triggers:
      1. First run (never thought before)
      2. Fallback timeout (THINK_FALLBACK_MINUTES elapsed)
      3. Observer delta detected
      4. Maintenance summary arrived
      5. Upcoming reminders need attention
    """
    global _last_think_at

    now = datetime.now(TZ)

    # Always think on first run
    if _last_think_at is None:
        return True

    # First tick of the day — always think (morning briefing)
    if observation.get("is_first_tick_today"):
        return True

    minutes_since = (now - _last_think_at).total_seconds() / 60

    # Fallback: think at least every N minutes
    if minutes_since >= _effective('THINK_FALLBACK_MINUTES'):
        return True

    # Delta: maintenance summary arrived
    if observation.get("maintenance_summary"):
        return True

    # Delta: upcoming reminders need attention
    if observation.get("upcoming_reminders"):
        return True

    # Delta: per-observer change detection
    if _check_observer_deltas(observation):
        return True

    return False


async def _think(observation: dict, user_id: int) -> dict | None:
    """Ask LLM to decide what to do based on observation.

    Returns result dict or None.
    Expected format: {"actions": [...], "thought": "..."}
    """
    global _last_think_at
    _last_think_at = datetime.now(TZ)

    prompt = get_prompt("think_system")
    if not prompt:
        log.warning("think_system prompt not found")
        return None

    obs_text = _build_observation_text(observation)

    client = get_client(purpose="think")
    response = await asyncio.to_thread(
        client.chat,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": obs_text},
        ],
        temperature=0.5,
        max_tokens=512,
    )

    log_usage(
        response.prompt_tokens, response.completion_tokens,
        response.total_tokens, model=response.model, purpose="heartbeat_think",
    )

    # Parse JSON result
    try:
        result = json.loads(response.content)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
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


def _build_observation_text(obs: dict) -> str:
    """Format observation dict into structured text for Think prompt."""
    sections = []

    # Time
    time_lines = (
        f"## Time\n"
        f"- {obs.get('timestamp', '?')}\n"
        f"- {obs.get('weekday', '?')}, {obs.get('time_of_day', '?')}"
    )
    if obs.get("is_first_tick_today"):
        time_lines += "\n- **First tick of the day** (morning briefing opportunity)"
    sections.append(time_lines)

    # Messages
    sections.append(
        f"## Messages\n"
        f"- Silence: {obs.get('silence_hours', '?')}h\n"
        f"- Messages today: {obs.get('messages_today', 0)}\n"
        f"- User status: {obs.get('user_status', 'unknown')}"
    )

    # Diary status (the key panel for habit/todo/reminder awareness)
    diary_status = obs.get("diary_status", "")
    if diary_status:
        sections.append(f"## Today Status\n{diary_status}")

    diary_journal = obs.get("diary_journal", "")
    if diary_journal:
        sections.append(f"## Today Journal\n{diary_journal}")

    # Core memory
    core = obs.get("core_memory_preview", "")
    if core:
        sections.append(f"## Core Memory\n{core}")

    # Maintenance summary
    maint = obs.get("maintenance_summary", "")
    if maint:
        sections.append(f"## Maintenance\n{maint}")

    # Upcoming reminders (within 2h)
    reminders = obs.get("upcoming_reminders", [])
    if reminders:
        lines = ["## Upcoming Reminders"]
        for r in reminders:
            lines.append(f"- {r.get('remind_at', '?')}: {r.get('message', '?')}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════════════════════
# Act — execute the Think decision
# ═══════════════════════════════════════════════════════════════════════════

async def _act(result: dict, user_id: int) -> None:
    """Execute the Think decision.

    Notify actions are batched and passed through chat_proactive() for
    persona-consistent expression. Other actions run directly.
    """
    global _last_proactive_at, _proactive_count_today, _last_proactive_date

    # Normalize: support both single-action and array formats
    actions = result.get("actions", [])
    if not actions and result.get("type"):
        actions = [result]

    thought = result.get("thought", "")
    if thought:
        log.info("Think thought: %s", thought[:120])

    if not actions:
        log_heartbeat(_state, "nothing")
        return

    # Separate notify actions from others
    notify_actions = []
    for action in actions:
        action_type = action.get("type", "nothing")

        if action_type == "nothing":
            log_heartbeat(_state, "nothing")

        elif action_type == "notify":
            notify_actions.append(action)

        elif action_type == "save_memory":
            from mochi.db import save_memory_item
            mem_content = action.get("content", "")
            if mem_content:
                save_memory_item(user_id, category="observation", content=mem_content)
                log_heartbeat(_state, "save_memory", mem_content[:100])

        elif action_type == "update_diary":
            try:
                from mochi.diary import diary
                entry = action.get("content", "")
                if entry:
                    diary.append(entry, source="think", section="今日日記")
                    log_heartbeat(_state, "update_diary", entry[:100])
            except Exception as e:
                log.warning("Diary update failed: %s", e)

        else:
            log.warning("Unknown action type: %s", action_type)
            log_heartbeat(_state, "unknown", str(action)[:200])

    # Dispatch notify actions through chat_proactive
    if not notify_actions:
        return

    await _dispatch_proactive(notify_actions, user_id)


async def _dispatch_proactive(notify_actions: list[dict], user_id: int) -> None:
    """Rate-limit, generate via chat_proactive, and deliver proactive messages."""
    global _last_proactive_at, _proactive_count_today, _last_proactive_date

    # Rate limiting (before LLM call to save tokens)
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if today != _last_proactive_date:
        _proactive_count_today = 0
        _last_proactive_date = today

    if _proactive_count_today >= _effective('MAX_DAILY_PROACTIVE'):
        log.info("Daily proactive limit reached (%d)", _effective('MAX_DAILY_PROACTIVE'))
        log_heartbeat(_state, "rate_limited")
        return

    if _last_proactive_at:
        elapsed = (now - _last_proactive_at).total_seconds()
        cooldown = _effective('PROACTIVE_COOLDOWN_SECONDS')
        if elapsed < cooldown:
            log.info("Proactive cooldown active (%ds remaining)", cooldown - elapsed)
            log_heartbeat(_state, "cooldown")
            return

    # Generate message via chat_proactive
    from mochi.ai_client import chat_proactive

    topics = [a.get("topic", "general") for a in notify_actions]
    topics_str = ",".join(topics)

    msg = await _llm_with_timeout(
        chat_proactive(notify_actions, user_id), "chat_proactive")

    if msg and msg != "[SKIP]":
        if _send_callback:
            await _send_callback(user_id, msg)
            _last_proactive_at = now
            _proactive_count_today += 1
            save_message(user_id, "assistant", msg)
            log_heartbeat(_state, f"proactive:{topics_str}", msg[:100])
            log.info("Proactive message sent [%s] (%d/%d today)",
                     topics_str, _proactive_count_today,
                     _effective('MAX_DAILY_PROACTIVE'))

            if any("maintenance" in (a.get("summary", "") + a.get("content", "")).lower()
                   for a in notify_actions):
                clear_maintenance_summary()
        else:
            log_heartbeat(_state, "notify_skipped", "no send callback")

    elif msg == "[SKIP]":
        log.info("Proactive skipped by LLM for: %s", topics_str)
        log_heartbeat(_state, f"proactive_skip:{topics_str}")

    else:
        log.warning("chat_proactive returned None for %d finding(s)", len(notify_actions))
        log_heartbeat(_state, "proactive_failed")


# ═══════════════════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════════════════

async def heartbeat_loop() -> None:
    """Main heartbeat loop. Run as asyncio task."""
    log.info("Heartbeat started: interval=%dm, awake=%d-%d, state=%s",
             HEARTBEAT_INTERVAL_MINUTES, AWAKE_HOUR_START, AWAKE_HOUR_END, _state)

    while True:
        try:
            interval = _effective('HEARTBEAT_INTERVAL_MINUTES') * 60

            # Re-read OWNER_USER_ID each cycle (may be auto-detected later)
            from mochi.config import OWNER_USER_ID as user_id
            if not user_id:
                log.debug("No owner set yet, heartbeat paused")
                await asyncio.sleep(interval)
                continue

            now = datetime.now(TZ)
            hour = now.hour

            # ── 1. Fallback wake check (MUST be before SLEEPING continue) ──
            if _state == SLEEPING:
                fallback_hour = _effective('FALLBACK_WAKE_HOUR')
                awake_end = _effective('AWAKE_HOUR_END')
                if fallback_hour <= hour < awake_end:
                    wake_up(f"fallback_{fallback_hour}:00")
                else:
                    log_heartbeat(_state, "sleeping")
                    await asyncio.sleep(interval)
                    continue

            # ── 2. Silence sleep check (AWAKE path) ──
            sleep_action = check_silence_sleep()
            if sleep_action:
                if _send_callback:
                    # Generate goodnight via chat_proactive for persona consistency
                    hint = sleep_action["context_hint"]
                    silence_h = sleep_action["silence_hours"]
                    re = "再次" if hint == "re_sleep" else ""
                    finding = {
                        "topic": "sleep_transition",
                        "summary": f"用户已沉默{silence_h}小时，深夜{re}静默，大概率睡着了",
                    }
                    from mochi.ai_client import chat_proactive
                    goodnight_msg = await _llm_with_timeout(
                        chat_proactive([finding], user_id), "goodnight")
                    if goodnight_msg and goodnight_msg != "[SKIP]":
                        await _send_callback(user_id, goodnight_msg)
                        save_message(user_id, "assistant", goodnight_msg)
                        log_heartbeat(_state, "silence_sleep", goodnight_msg[:100])
                go_to_sleep("silence_detected")
                await asyncio.sleep(interval)
                continue

            # ── 3. Silent pause check ──
            _check_silence_pause()
            if _silent_pause:
                log.debug("Silent pause active — tick suppressed")
                log_heartbeat(_state, "silent_pause")
                await asyncio.sleep(interval)
                continue

            # ── 4. Morning hold: suppress proactive but still observe/maintain ──
            # (We continue the loop so maintenance can still run,
            #  but skip Think/proactive actions)

            # Nightly maintenance (runs once per day at MAINTENANCE_HOUR)
            await _llm_with_timeout(_run_maintenance_if_due(user_id), "maintenance")

            # Observe (cheap: no LLM)
            observation = await _observe(user_id)

            # Morning hold: skip Think/proactive (but observation ran)
            if _morning_hold:
                log_heartbeat(_state, "morning_hold")
                await asyncio.sleep(interval)
                continue

            # Think (only if delta or fallback)
            if _should_think(observation):
                action = await _llm_with_timeout(
                    _think(observation, user_id), "think")
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
