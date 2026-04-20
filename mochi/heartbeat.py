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
    WAKE_EARLIEST_HOUR, SLEEP_AFTER_HOUR, SILENCE_THRESHOLD_HOURS,
    TZ,
    OWNER_USER_ID,
    logical_today,
)
from mochi.llm import get_client_for_tier, extract_json
from mochi.prompt_loader import get_prompt
from mochi.db import (
    log_heartbeat,
    get_last_heartbeat_log,
    get_core_memory,
    get_last_user_message_time,
    get_message_count_today,
    get_recent_messages,
    save_message,
    log_usage,
    log_proactive,
)
from mochi.runtime_state import (
    get_maintenance_summary,
    clear_maintenance_summary,
)

log = logging.getLogger(__name__)


def _format_silence(silence_hours: float | int | None) -> str:
    """Format silence_hours into a natural-language Chinese label.

    Returns labels like '刚刚' / '13分钟前' / '2小时前' / '3天前'.
    Defensive against None / non-numeric / negative values (clock skew).
    """
    if not isinstance(silence_hours, (int, float)):
        return "未知"
    silence_mins = int(max(0, silence_hours) * 60)
    if silence_mins < 2:
        return "刚刚"
    if silence_mins < 60:
        return f"{silence_mins}分钟前"
    hours = silence_mins // 60
    if hours < 24:
        return f"{hours}小时前"
    return f"{hours // 24}天前"


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


# ── Runtime config (DB-only, cached in admin_db) ─────────────────────────


def _effective(key: str):
    """Get effective config value from DB (cached 60s in admin_db)."""
    from mochi.admin.admin_db import get_system_config
    return get_system_config(key)


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
    if WAKE_EARLIEST_HOUR <= hour < SLEEP_AFTER_HOUR:
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
    global _state, _state_changed_at, _wake_reason
    global _prev_observer_raw, _last_think_at
    if _state == SLEEPING:
        _state = AWAKE
        _state_changed_at = datetime.now(TZ)
        _wake_reason = reason
        _prev_observer_raw = {}
        _last_think_at = None
        _persist_state(AWAKE, _state_changed_at)
        log.info("WAKE UP — reason: %s", reason)


def go_to_sleep(reason: str = "unknown") -> None:
    """Transition AWAKE → SLEEPING."""
    global _state, _state_changed_at, _wake_reason
    global _prev_observer_raw, _last_sleep_at
    if _state == AWAKE:
        _state = SLEEPING
        _state_changed_at = datetime.now(TZ)
        _last_sleep_at = _state_changed_at
        _wake_reason = None
        _prev_observer_raw = {}
        _persist_state(SLEEPING, _state_changed_at)
        log.info("SLEEPING — reason: %s", reason)


def force_wake() -> None:
    """Backward-compatible wake — delegates to wake_up("user_message")."""
    wake_up("user_message")


def should_wake_on_message() -> bool:
    """Check if a user message should wake the bot.

    Only wakes if current hour >= WAKE_EARLIEST_HOUR (default 6 AM).
    Before that, user messages are received but don't trigger wake.
    """
    if _state != SLEEPING:
        return False
    return datetime.now(TZ).hour >= WAKE_EARLIEST_HOUR


def check_sleep_entry(last_user_msg_text: str | None = None) -> bool:
    """Check if user is going to sleep via goodnight keywords.

    Called by transport to decide whether to route to bedtime tidy
    instead of normal Chat.  Only triggers during the night window
    (SLEEP_AFTER_HOUR..WAKE_EARLIEST_HOUR) to avoid false positives.

    Returns True if sleep keyword detected (caller should call
    handle_sleep_keyword instead of the normal chat path).
    """
    if _state != AWAKE or not last_user_msg_text:
        return False

    hour = datetime.now(TZ).hour
    if not (hour >= SLEEP_AFTER_HOUR or hour < WAKE_EARLIEST_HOUR):
        return False

    text_lower = last_user_msg_text.lower().strip()
    if any(kw in text_lower for kw in _effective('SLEEP_KEYWORDS').split(",")):
        return True

    return False


async def handle_sleep_keyword(user_id: int, text: str = "") -> None:
    """Run bedtime tidy then transition to SLEEPING.

    Called by transport layer when check_sleep_entry() returns True,
    *instead of* the normal Chat path.  Saves the user message here
    because chat() is bypassed.
    """
    if text:
        save_message(user_id, "user", text)
    await _run_bedtime_tidy(user_id, reason="keyword")
    go_to_sleep(reason="keyword")


async def _run_bedtime_tidy(user_id: int, reason: str = "unknown") -> None:
    """Run the bedtime tidy routine if enabled.

    Gathers today's findings and passes them to chat_bedtime_tidy(),
    which uses tools (notes, todos) to clean up before saying goodnight.
    """
    if not _effective('BEDTIME_TIDY_ENABLED'):
        return

    try:
        from mochi.ai_client import chat_bedtime_tidy
        from mochi.diary import diary

        findings = []

        # Include full diary (今日状態 + 今日日記) as findings context
        diary_full = diary.read()
        if diary_full:
            findings.append({
                "topic": "today_diary",
                "summary": diary_full[:800],
            })

        findings.append({
            "topic": "sleep_transition",
            "summary": f"Sleep reason: {reason}",
        })

        tidy_msg = await asyncio.wait_for(
            chat_bedtime_tidy(findings, user_id),
            timeout=_effective('BEDTIME_TIDY_TIMEOUT_S'),
        )

        if tidy_msg and tidy_msg != "[SKIP]" and _send_callback:
            await _send_callback(user_id, tidy_msg)
            save_message(user_id, "assistant", tidy_msg)
            log_heartbeat(_state, "bedtime_tidy", tidy_msg[:100])
            log.info("Bedtime tidy complete: %s", tidy_msg[:60])
        elif tidy_msg == "[SKIP]":
            log.info("Bedtime tidy vetoed by LLM")

    except asyncio.TimeoutError:
        log.warning("Bedtime tidy timed out after %ds", _effective('BEDTIME_TIDY_TIMEOUT_S'))
    except Exception as e:
        log.error("Bedtime tidy failed: %s", e, exc_info=True)


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

    # Only during night window: SLEEP_AFTER_HOUR..midnight..WAKE_EARLIEST_HOUR
    if not (hour >= SLEEP_AFTER_HOUR or hour < WAKE_EARLIEST_HOUR):
        return None

    # Check silence duration
    from mochi.config import OWNER_USER_ID as user_id
    if user_id is None:
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

    if silence_hours < SILENCE_THRESHOLD_HOURS:
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
                 _effective('SILENCE_PAUSE_DAYS'))


def clear_silent_pause() -> None:
    """Exit silent pause: user just sent a message."""
    global _silent_pause
    if _silent_pause:
        _silent_pause = False
        log.info("SILENT PAUSE cleared — user returned")


def _check_silence_pause() -> None:
    """Check if we should enter/exit silent pause based on last message time."""
    from mochi.config import OWNER_USER_ID as user_id
    if user_id is None:
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

    threshold_hours = _effective('SILENCE_PAUSE_DAYS') * 24
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
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=TZ)
            silence_hours = (now - last_dt).total_seconds() / 3600
            observation["silence_hours"] = round(silence_hours, 1)
        except (ValueError, TypeError):
            observation["silence_hours"] = None
    else:
        observation["silence_hours"] = None

    # Conversation activity today
    msg_count = get_message_count_today(user_id)
    observation["messages_today"] = msg_count

    # Core memory is injected into system prompt by _think(), not here.
    # Keeping it out of observation avoids duplication.

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

    # Notes (persistent working memory — via prompt section hook)
    import mochi.skills as skill_registry
    for section in skill_registry.get_prompt_sections(compact=True):
        observation["notes"] = section

    # First tick of the day (for Think morning awareness)
    from mochi.db import get_awake_tick_count_today
    observation["is_first_tick_today"] = get_awake_tick_count_today() == 0

    # Today's proactive messages (so Think knows what it already said)
    try:
        from mochi.db import get_today_proactive_sent
        observation["today_proactive_sent"] = get_today_proactive_sent()
    except Exception as e:
        log.warning("Proactive log read failed: %s", e)

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
    today = logical_today(now)
    if now.hour != _effective('MAINTENANCE_HOUR') or today == _last_maintenance_date:
        return False

    _last_maintenance_date = today
    log.info("Running nightly maintenance...")

    try:
        import mochi.skills as skill_registry
        from mochi.skills.base import SkillContext
        maint = skill_registry.get_skill("maintenance")
        if maint:
            ctx = SkillContext(trigger="cron", user_id=user_id)
            result = await maint.run(ctx)
            log_heartbeat(_state, "maintenance", result.output[:200])
        else:
            log.warning("Maintenance skill not found, skipping")
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

    # Delta: upcoming reminders need attention (via observer)
    obs_data = observation.get("observers", {})
    if obs_data.get("reminder", {}).get("upcoming"):
        return True

    # Delta: per-observer change detection
    if _check_observer_deltas(observation):
        return True

    return False


async def _think(observation: dict, user_id: int) -> dict | None:
    """Ask LLM to scan responsibility zones and output findings.

    Think is a scanner/triage view — no soul, no message authorship.
    Reads observation + conversation history + diary + notes, outputs
    {"thought": "...", "findings": [...], "side_effects": [...]}.
    Expression is delegated to chat_proactive (which has soul + full context).
    """
    global _last_think_at
    _last_think_at = datetime.now(TZ)

    # ── Build system prompt: think instructions + time + core memory ──
    think_template = get_prompt("think_system")
    if not think_template:
        log.warning("think_system prompt not found")
        return None

    system_prompt = think_template

    now = datetime.now(TZ)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S %z")
    system_prompt += f"\n\n当前时间：{now_str}"

    core_memory = get_core_memory(user_id)
    if core_memory:
        system_prompt += f"\n\n## 你对用户的了解\n{core_memory}"

    # ── Build user message: observation + recent conversation ──
    obs_text = _build_observation_text(observation)

    recent = get_recent_messages(user_id, limit=_effective('THINK_HISTORY_TURNS'))
    if recent:
        conv_lines = []
        for m in recent:
            role = "用户" if m.get("role") == "user" else "你"
            content = (m.get("content") or "")[:200]
            conv_lines.append(f"- {role}: {content}")
        obs_text += "\n\n## 最近的互动记录\n" + "\n".join(conv_lines)

    client = get_client_for_tier("deep")
    response = await asyncio.to_thread(
        client.chat,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": obs_text},
        ],
        max_tokens=_effective('THINK_MAX_TOKENS'),
        json_mode=True,
    )

    log_usage(
        response.prompt_tokens, response.completion_tokens,
        response.total_tokens, model=response.model, purpose="heartbeat_think",
        reasoning_tokens=response.reasoning_tokens,
        cached_prompt_tokens=response.cached_prompt_tokens,
    )

    # Parse JSON result. Provider layer enforces JSON output natively
    # (response_format / response_mime_type) and strips any markdown fence
    # plus reasoning XML wrappers via extract_json. The extra extract_json
    # call here is defense-in-depth for third-party gateways that may not
    # honor response_format.
    try:
        result = json.loads(extract_json(response.content))
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError) as e:
        log.warning("Think response was not valid JSON: %s | raw: %s",
                    e, (response.content or "")[:500])
    return None


def _build_observation_text(obs: dict) -> str:
    """Format observation dict into structured text for Think prompt.

    Layout follows LLM primacy/recency attention pattern:
      Block 1 — Operational/alerts (sparse, low attention)
      Block 2 — Messages & meta (transition)
      Block 3 — Core context (tail = highest recency attention)
    """
    sections = []

    # ── Block 1: 运维/告警 (lowest attention) ────────────────────

    # Maintenance summary
    maint = obs.get("maintenance_summary", "")
    if maint:
        sections.append(f"## 系统维护\n{maint}")

    # Upcoming reminders (within 2h, from observer)
    obs_data = obs.get("observers", {})
    reminders = obs_data.get("reminder", {}).get("upcoming", [])
    if reminders:
        lines = ["## 即将到来的提醒"]
        for r in reminders:
            lines.append(f"- {r.get('remind_at', '?')}: {r.get('message', '?')}")
        sections.append("\n".join(lines))

    # ── Block 2: 消息/元数据 (transition) ────────────────────────

    # Messages
    sections.append(
        f"## 消息\n"
        f"- 用户上次开口: {_format_silence(obs.get('silence_hours'))}\n"
        f"- 今日消息数: {obs.get('messages_today', 0)}"
    )

    # Today's sent proactive messages (for repeat avoidance)
    sent = obs.get("today_proactive_sent", [])
    if sent:
        lines = ["## 今日已发消息"]
        for s in sent:
            content_preview = s.get("content", "")[:40]
            topic = s.get("type", "?")
            time_str = s.get("time", "?")
            lines.append(f"- [{topic}] {content_preview} ({time_str})")
        sections.append("\n".join(lines))

    # ── Block 3: 核心上下文 (highest recency attention) ──────────

    # Core memory is in system prompt, not duplicated here.

    # Diary status (the key panel for habit/todo/reminder awareness)
    diary_status = obs.get("diary_status", "")
    if diary_status:
        sections.append(f"## 今日状态\n{diary_status}")

    diary_journal = obs.get("diary_journal", "")
    if diary_journal:
        sections.append(f"## 今日日记\n{diary_journal}")

    # Notes (persistent working memory from notes.md)
    notes = obs.get("notes", "")
    if notes:
        sections.append(notes)

    # Time — absolute last for maximum recency bias
    time_lines = (
        f"## 时间\n"
        f"- {obs.get('timestamp', '?')}\n"
        f"- {obs.get('weekday', '?')}, {obs.get('time_of_day', '?')}"
    )
    if obs.get("is_first_tick_today"):
        time_lines += "\n- **今天第一次巡逻**（可发送 morning briefing）"
    sections.append(time_lines)

    return "\n\n".join(sections)


# ═══════════════════════════════════════════════════════════════════════════
# Act — execute the Think decision
# ═══════════════════════════════════════════════════════════════════════════

async def _act(result: dict, user_id: int) -> None:
    """Execute the Think decision.

    Result schema: {"thought": "...", "findings": [...], "side_effects": [...]}
    - side_effects execute unconditionally (diary/note/skill)
    - findings (if non-empty) are dispatched to chat_proactive for soul-driven
      expression. Empty findings = silent tick, no chat call.
    """
    thought = result.get("thought", "")
    if thought:
        log.info("Think thought: %s", thought[:200])

    # 1. Execute side_effects unconditionally
    for effect in result.get("side_effects", []):
        etype = effect.get("type", "")

        if etype == "update_diary":
            try:
                from mochi.diary import diary
                entry = effect.get("content", "")
                if entry:
                    diary.append(entry, source="think", section="今日日記")
                    log_heartbeat(_state, "update_diary", entry[:100])
            except Exception as e:
                log.warning("Diary update failed: %s", e)

        elif etype == "manage_note":
            note_action = effect.get("action", "")
            raw_nid = effect.get("note_id")
            if note_action == "remove" and raw_nid is not None:
                try:
                    from mochi.skills import skill_for_tool, get_skill
                    from mochi.skills.base import SkillContext
                    sname = skill_for_tool("manage_note")
                    skill = get_skill(sname) if sname else None
                    if skill:
                        ctx = SkillContext(
                            trigger="heartbeat", user_id=user_id,
                            tool_name="manage_note",
                            args={"action": "remove", "note_id": int(raw_nid)},
                        )
                        sr = await skill.run(ctx)
                        log_heartbeat(_state, "manage_note",
                                      f"remove #{raw_nid}: {sr.output[:80]}")
                    else:
                        log.warning("manage_note: note skill not found")
                except Exception as e:
                    log.error("manage_note error: %s", e)

        elif etype == "run_skill":
            skill_name = effect.get("skill", "")
            skill_args = effect.get("args", {})
            try:
                from mochi.skills import get_skill
                from mochi.skills.base import SkillContext
                skill = get_skill(skill_name)
                if not skill:
                    log.warning("run_skill: skill %r not found", skill_name)
                else:
                    ctx = SkillContext(
                        trigger="heartbeat", user_id=user_id, args=skill_args,
                    )
                    sr = await skill.run(ctx)
                    log_heartbeat(_state, f"run_skill:{skill_name}",
                                  sr.output[:80])
            except Exception as e:
                log.error("run_skill(%s) error: %s", skill_name, e,
                          exc_info=True)

        else:
            log.warning("Unknown side_effect type: %s", etype)

    # 2. Dispatch findings to chat_proactive (soul-driven expression)
    findings = result.get("findings", [])
    if not findings:
        log_heartbeat(_state, "think_silent")
        return

    await _dispatch_proactive(findings, user_id)


async def _dispatch_proactive(findings: list[dict], user_id: int) -> None:
    """Rate-limit, generate via chat_proactive (soul演绎), and deliver.

    Single delivery funnel for all proactive messages. Findings are passed
    to chat_proactive which renders them in the bot's voice with full
    chat-side context (soul + diary + memory + history).
    """
    global _last_proactive_at, _proactive_count_today, _last_proactive_date

    # Rate limiting (before LLM call to save tokens)
    now = datetime.now(TZ)
    today = logical_today(now)
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

    # Annotate each finding with how many times its topic was already
    # surfaced today. Chat uses this to drive persona-specific behaviour:
    # 强势人格 escalates tone on repeats; 温和 backs off or [SKIP]s.
    # Source data: proactive_log topic strings (may be comma-joined when a
    # past tick had multiple findings; we split before counting).
    from mochi.db import get_today_proactive_sent
    sent_today = get_today_proactive_sent()
    topic_counts: dict[str, int] = {}
    for entry in sent_today:
        for t in (entry.get("type") or "").split(","):
            t = t.strip()
            if t:
                topic_counts[t] = topic_counts.get(t, 0) + 1
    for f in findings:
        topic = f.get("topic", "")
        if topic and topic_counts.get(topic, 0) > 0:
            f["prior_attempts"] = topic_counts[topic]

    # Generate message via chat_proactive
    from mochi.ai_client import chat_proactive

    topics = [a.get("topic", "general") for a in findings]
    topics_str = ",".join(topics)

    try:
        msg = await _llm_with_timeout(
            chat_proactive(findings, user_id), "chat_proactive")
    except Exception as e:
        log.error("chat_proactive exception: %s", e, exc_info=True)
        log_heartbeat(_state, "proactive_failed", f"exception: {e}"[:200])
        return

    if msg and msg != "[SKIP]":
        if _send_callback:
            await _send_callback(user_id, msg)
            _last_proactive_at = now
            _proactive_count_today += 1
            save_message(user_id, "assistant", msg)
            log_proactive(msg, topics_str)
            log_heartbeat(_state, f"proactive:{topics_str}", msg[:100])
            log.info("Proactive message sent [%s] (%d/%d today)",
                     topics_str, _proactive_count_today,
                     _effective('MAX_DAILY_PROACTIVE'))

            if any("maintenance" in (a.get("summary", "") + a.get("content", "")).lower()
                   for a in findings):
                clear_maintenance_summary()
        else:
            log_heartbeat(_state, "notify_skipped", "no send callback")

    elif msg == "[SKIP]":
        log.info("Proactive skipped by LLM for: %s", topics_str)
        log_heartbeat(_state, f"proactive_skip:{topics_str}")

    else:
        log.warning("chat_proactive returned None for %d finding(s)", len(findings))
        log_heartbeat(_state, "proactive_failed", "returned None")


# ═══════════════════════════════════════════════════════════════════════════
# Main Loop
# ═══════════════════════════════════════════════════════════════════════════

async def heartbeat_loop() -> None:
    """Main heartbeat loop. Run as asyncio task."""
    log.info("Heartbeat started: interval=%dm, wake_after=%d, sleep_after=%d, state=%s",
             _effective('HEARTBEAT_INTERVAL_MINUTES'), WAKE_EARLIEST_HOUR,
             SLEEP_AFTER_HOUR, _state)

    while True:
        try:
            interval = _effective('HEARTBEAT_INTERVAL_MINUTES') * 60

            # Re-read OWNER_USER_ID each cycle (may be auto-detected later)
            from mochi.config import OWNER_USER_ID as user_id
            if user_id is None:
                log.debug("No owner set yet, heartbeat paused")
                await asyncio.sleep(interval)
                continue

            now = datetime.now(TZ)
            hour = now.hour

            # ── 1. Fallback wake check (MUST be before SLEEPING continue) ──
            if _state == SLEEPING:
                fallback_hour = _effective('FALLBACK_WAKE_HOUR')
                if fallback_hour <= hour < SLEEP_AFTER_HOUR:
                    wake_up(f"fallback_{fallback_hour}:00")
                else:
                    log_heartbeat(_state, "sleeping")
                    await asyncio.sleep(interval)
                    continue

            # ── 2. Silence sleep check (AWAKE path) ──
            sleep_action = check_silence_sleep()
            if sleep_action:
                hint = sleep_action["context_hint"]
                silence_h = sleep_action["silence_hours"]
                re_tag = "再次" if hint == "re_sleep" else ""
                # Send a natural goodnight via chat_proactive (no bedtime tidy —
                # user didn't say goodnight, just went quiet)
                if _send_callback and _state == AWAKE:
                    finding = {
                        "topic": "sleep_transition",
                        "summary": (
                            f"用户已经{re_tag}沉默了{silence_h}小时，深夜了。"
                            f"大概率睡着了或者不想聊了。"
                            f"用你自己的方式随意地说一句晚安，就像朋友之间一样自然。"
                        ),
                    }
                    from mochi.ai_client import chat_proactive
                    goodnight_msg = await _llm_with_timeout(
                        chat_proactive([finding], user_id), "goodnight")
                    if goodnight_msg and goodnight_msg != "[SKIP]":
                        await _send_callback(user_id, goodnight_msg)
                        save_message(user_id, "assistant", goodnight_msg)
                        log_proactive(goodnight_msg, "sleep_transition")
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
