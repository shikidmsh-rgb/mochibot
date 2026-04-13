"""Habit skill handler — recurring habit tracking with check-in, pause, and stats.

Port of internal project's habit skill, adapted to MochiBot conventions:
- SkillContext / SkillResult pattern
- TZ from mochi.config
- No nightly settlement (Phase 2)
- No diary integration (Phase 2)
"""

import logging
import sqlite3
from datetime import datetime, timedelta

from mochi.config import TZ, logical_today
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.habit.logic import parse_frequency, get_allowed_days
from mochi.db import (
    add_habit,
    list_habits,
    deactivate_habit,
    update_habit,
    checkin_habit,
    get_habit_checkins,
    delete_habit_checkin,
    get_habit_stats,
    get_habit_streak,
    pause_habit,
    resume_habit,
)

log = logging.getLogger(__name__)

_DAY_LABEL_CN = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}


def _format_allowed_days(days: set[int]) -> str:
    """Format allowed days as Chinese label, e.g. '六日'."""
    return "".join(_DAY_LABEL_CN[d] for d in sorted(days))


def _current_period(cycle: str) -> str:
    """Return the current period string: 'YYYY-MM-DD' for daily, 'YYYY-WNN' for weekly."""
    now = datetime.now(TZ)
    if cycle == "daily":
        return logical_today(now)
    else:
        return now.strftime("%G-W%V")


def _is_paused(habit: dict) -> bool:
    """Check if a habit is currently paused (paused_until >= today)."""
    paused_until = habit.get("paused_until")
    if not paused_until:
        return False
    today = logical_today()
    return paused_until >= today


class HabitSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        """Dispatch by tool_name + action."""
        args = context.args
        action = args.get("action", "")
        uid = context.user_id

        if context.tool_name == "query_habit":
            if action == "list":
                return self._list(uid)
            elif action == "stats":
                return self._stats(uid, args)
            return SkillResult(output=f"Unknown query_habit action: {action}", success=False)

        elif context.tool_name == "checkin_habit":
            if action == "checkin":
                return self._checkin(uid, args)
            elif action == "undo_checkin":
                return self._undo_checkin(uid, args)
            return SkillResult(output=f"Unknown checkin_habit action: {action}", success=False)

        elif context.tool_name == "edit_habit":
            if action == "add":
                return self._add(uid, args)
            elif action == "remove":
                return self._remove(uid, args)
            elif action == "pause":
                return self._pause(uid, args)
            elif action == "resume":
                return self._resume(uid, args)
            elif action == "update":
                return self._update(uid, args)
            return SkillResult(output=f"Unknown edit_habit action: {action}", success=False)

        return SkillResult(output=f"Unknown habit tool: {context.tool_name}", success=False)

    # ── edit_habit actions ───────────────────────────────────────────────

    def _add(self, user_id: int, args: dict) -> SkillResult:
        name = args.get("name")
        frequency = args.get("frequency")
        if not name:
            return SkillResult(output="Error: 'name' is required for add.", success=False)
        if not frequency:
            return SkillResult(output="Error: 'frequency' is required (e.g. 'daily:2' or 'weekly:3').", success=False)
        parsed = parse_frequency(frequency)
        if not parsed:
            return SkillResult(
                output=f"Error: invalid frequency '{frequency}'. "
                       "Use 'daily:N', 'weekly:N', or 'weekly_on:DAY,...:N' "
                       "(e.g. 'weekly_on:sat,sun:1').",
                success=False,
            )
        cycle, target = parsed
        allowed_days = get_allowed_days(frequency)
        category = args.get("category", "")
        importance = args.get("importance", "normal")
        if importance not in ("important", "normal"):
            importance = "normal"
        context = args.get("context", "")

        try:
            hid = add_habit(
                user_id=user_id, name=name, frequency=frequency,
                category=category, importance=importance, context=context,
            )
        except sqlite3.IntegrityError:
            return SkillResult(output=f"Error: habit '{name}' already exists.", success=False)

        if allowed_days is not None:
            cycle_label = f"every week on {_format_allowed_days(allowed_days)}"
        elif cycle == "daily":
            cycle_label = "daily"
        else:
            cycle_label = "weekly"
        imp_label = " ⚡important" if importance == "important" else ""
        ctx_label = f" ({context})" if context else ""
        return SkillResult(
            output=f"Habit #{hid} created{imp_label}: {name} ({cycle_label} x{target}){ctx_label}"
                   f"{f' [{category}]' if category else ''}"
        )

    def _remove(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for remove.", success=False)
        ok = deactivate_habit(user_id, int(habit_id))
        return SkillResult(
            output=f"Habit #{habit_id} deactivated." if ok else f"Habit #{habit_id} not found.",
            success=ok,
        )

    def _pause(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for pause.", success=False)
        until = args.get("until", "")
        if not until:
            until = (datetime.now(TZ) + timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            return SkillResult(output=f"Error: invalid date '{until}', use YYYY-MM-DD.", success=False)
        ok = pause_habit(user_id, int(habit_id), until)
        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        habits = list_habits(user_id)
        habit = next((h for h in habits if h["id"] == int(habit_id)), None)
        name = habit["name"] if habit else f"#{habit_id}"
        return SkillResult(output=f"⏸️ {name} paused until {until}.")

    def _resume(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for resume.", success=False)
        ok = resume_habit(user_id, int(habit_id))
        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        habits = list_habits(user_id)
        habit = next((h for h in habits if h["id"] == int(habit_id)), None)
        name = habit["name"] if habit else f"#{habit_id}"
        return SkillResult(output=f"▶️ {name} resumed.")

    def _update(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for update.", success=False)

        fields = {}
        for key in ("name", "context", "importance", "frequency"):
            if key in args and args[key] is not None:
                fields[key] = args[key]
        if not fields:
            return SkillResult(
                output="Error: provide at least one field to update (name, context, importance, frequency).",
                success=False,
            )

        # Validate frequency if being updated
        if "frequency" in fields:
            parsed = parse_frequency(fields["frequency"])
            if not parsed:
                return SkillResult(
                    output=f"Error: invalid frequency '{fields['frequency']}'.",
                    success=False,
                )

        # Validate importance if being updated
        if "importance" in fields and fields["importance"] not in ("important", "normal"):
            return SkillResult(output="Error: importance must be 'important' or 'normal'.", success=False)

        try:
            ok = update_habit(int(habit_id), **fields)
        except sqlite3.IntegrityError:
            return SkillResult(output=f"Error: habit name '{fields.get('name')}' already exists.", success=False)

        if not ok:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)
        parts = ", ".join(f"{k}={v}" for k, v in fields.items())
        return SkillResult(output=f"Habit #{habit_id} updated: {parts}.")

    # ── query_habit actions ──────────────────────────────────────────────

    def _list(self, user_id: int) -> SkillResult:
        habits = list_habits(user_id)
        if not habits:
            return SkillResult(output="No active habits.")

        now = datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        this_week = now.strftime("%G-W%V")
        weekday = now.weekday()

        lines = []
        for h in habits:
            if _is_paused(h):
                lines.append(f"#{h['id']} ⏸️ {h['name']} — paused until {h['paused_until']}")
                continue
            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed
            period = today if cycle == "daily" else this_week
            checkins = get_habit_checkins(h["id"], period)
            done = len(checkins)
            mark = "✅" if done >= target else "⬜"
            progress = f"{done}/{target}"
            imp = " ⚡" if h["importance"] == "important" else ""
            cat = f" [{h['category']}]" if h.get("category") else ""
            ctx = f" ({h['context']})" if h.get("context") else ""

            allowed = get_allowed_days(h["frequency"])
            day_hint = ""
            if allowed is not None:
                day_hint = f" 📅{_format_allowed_days(allowed)}"
                if weekday not in allowed:
                    day_hint += "(not active today)"

            streak_tag = ""
            if h["importance"] != "important":
                streak = get_habit_streak(h["id"], cycle, target, allowed)
                unit = "d" if cycle == "daily" else "w"
                streak_tag = f" 🔥{streak}{unit}" if streak > 0 else ""

            lines.append(f"#{h['id']} {mark} {h['name']}{imp}{cat}{ctx}{day_hint} — {progress}{streak_tag}")

        return SkillResult(output="\n".join(lines) if lines else "No active habits.")

    def _stats(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        habits = list_habits(user_id)
        target_habits = [h for h in habits if (not habit_id or h["id"] == int(habit_id))]
        if not target_habits:
            return SkillResult(output="No habits found.")

        now = datetime.now(TZ)
        lines = []
        for h in target_habits:
            if _is_paused(h):
                lines.append(f"#{h['id']} {h['name']} — ⏸️ paused until {h['paused_until']}")
                continue
            parsed = parse_frequency(h["frequency"])
            if not parsed:
                continue
            cycle, target = parsed

            if cycle == "daily":
                periods = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
                stats = get_habit_stats(h["id"], periods)
                completed_days = sum(1 for p in periods if stats.get(p, 0) >= target)
                marks = "".join("✅" if stats.get(p, 0) >= target else "❌" for p in reversed(periods))
                streak_tag = ""
                if h["importance"] != "important":
                    allowed = get_allowed_days(h["frequency"])
                    streak = get_habit_streak(h["id"], cycle, target, allowed)
                    streak_tag = f" 🔥{streak}d streak" if streak > 0 else ""
                lines.append(f"#{h['id']} {h['name']} — 7d: {marks} ({completed_days}/7){streak_tag}")
            else:
                periods = [(now - timedelta(weeks=i)).strftime("%G-W%V") for i in range(4)]
                stats = get_habit_stats(h["id"], periods)
                completed_weeks = sum(1 for p in periods if stats.get(p, 0) >= target)
                marks = "".join("✅" if stats.get(p, 0) >= target else "❌" for p in reversed(periods))
                streak_tag = ""
                if h["importance"] != "important":
                    streak = get_habit_streak(h["id"], cycle, target)
                    streak_tag = f" 🔥{streak}w streak" if streak > 0 else ""
                lines.append(f"#{h['id']} {h['name']} — 4w: {marks} ({completed_weeks}/4){streak_tag}")

        return SkillResult(output="\n".join(lines) if lines else "No stats available.")

    # ── checkin_habit actions ────────────────────────────────────────────

    def _checkin(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for checkin.", success=False)

        habits = list_habits(user_id)
        habit = next((h for h in habits if h["id"] == int(habit_id)), None)
        if not habit:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)

        parsed = parse_frequency(habit["frequency"])
        if not parsed:
            return SkillResult(output=f"Error: invalid frequency on habit #{habit_id}.", success=False)
        cycle, target = parsed
        period = _current_period(cycle)
        note = args.get("note", "")
        count = max(1, int(args.get("count", 1) or 1))

        existing = get_habit_checkins(int(habit_id), period)
        if len(existing) >= target:
            cycle_label = "today" if cycle == "daily" else "this week"
            return SkillResult(output=f"{habit['name']} already completed {target}x {cycle_label}! 🎉")

        # Write checkins, stopping at target
        slots_left = target - len(existing)
        actual = min(count, slots_left)
        for _ in range(actual):
            checkin_habit(int(habit_id), user_id, period, note)
        done = len(existing) + actual
        remaining = target - done

        # Refresh diary status so habit progress is immediately visible
        try:
            from mochi.diary import refresh_diary_status
            refresh_diary_status()
        except Exception:
            pass

        extra = f" (x{actual})" if actual > 1 else ""
        if remaining == 0:
            return SkillResult(output=f"✅ {habit['name']} completed! ({done}/{target}) 🎉{extra}")
        cycle_label = "today" if cycle == "daily" else "this week"
        return SkillResult(output=f"✅ {habit['name']} checked in {done}/{target}, {remaining} left {cycle_label}{extra}")

    def _undo_checkin(self, user_id: int, args: dict) -> SkillResult:
        habit_id = args.get("habit_id")
        if not habit_id:
            return SkillResult(output="Error: 'habit_id' is required for undo_checkin.", success=False)

        habits = list_habits(user_id)
        habit = next((h for h in habits if h["id"] == int(habit_id)), None)
        if not habit:
            return SkillResult(output=f"Habit #{habit_id} not found.", success=False)

        parsed = parse_frequency(habit["frequency"])
        if not parsed:
            return SkillResult(output=f"Error: invalid frequency on habit #{habit_id}.", success=False)
        cycle, target = parsed
        period = _current_period(cycle)
        existing = get_habit_checkins(int(habit_id), period)
        if not existing:
            cycle_label = "today" if cycle == "daily" else "this week"
            return SkillResult(output=f"{habit['name']} has no checkins {cycle_label} — nothing to undo.")

        latest = existing[-1]
        delete_habit_checkin(latest["id"])
        remaining = len(existing) - 1
        return SkillResult(output=f"↩️ {habit['name']} last checkin undone ({remaining}/{target})")

