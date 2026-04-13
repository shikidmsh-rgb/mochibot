"""Reminder skill — create, list, and delete reminders via unified tool."""

from datetime import datetime, date as date_type

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import create_reminder, get_pending_reminders, mark_reminder_fired


class ReminderSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action", "list")
        uid = context.user_id

        if action == "create":
            message = args.get("message", "")
            remind_at = args.get("remind_at", "")
            if not message or not remind_at:
                return SkillResult(output="Need both message and remind_at.", success=False)
            rid = create_reminder(uid, context.channel_id, message, remind_at)
            return SkillResult(output=f"Reminder #{rid} set for {remind_at}: {message}")

        elif action == "list":
            reminders = get_pending_reminders()
            user_reminders = [r for r in reminders if r["user_id"] == uid]
            if not user_reminders:
                return SkillResult(output="No pending reminders.")
            lines = [f"- #{r['id']} [{r['remind_at']}] {r['message']}" for r in user_reminders]
            return SkillResult(output=f"{len(user_reminders)} reminders:\n" + "\n".join(lines))

        elif action == "delete":
            rid = args.get("reminder_id")
            if not rid:
                return SkillResult(output="Need reminder_id to delete.", success=False)
            try:
                mark_reminder_fired(int(rid))
            except (ValueError, TypeError):
                return SkillResult(output=f"Invalid reminder_id: {rid}", success=False)
            return SkillResult(output=f"Reminder #{rid} deleted.")

        return SkillResult(output=f"Unknown action: {action}", success=False)

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.db import _connect
        from mochi.config import TZ

        # Query all unfired reminders for today (including future times)
        conn = _connect()
        rows = conn.execute(
            "SELECT message, remind_at, fired FROM reminders "
            "WHERE user_id = ? AND remind_at >= ? AND remind_at < ? "
            "ORDER BY remind_at",
            (user_id, today, today + "T99"),  # date prefix range
        ).fetchall()
        conn.close()

        if not rows:
            return None

        lines: list[str] = []
        for r in rows:
            try:
                remind_at = datetime.fromisoformat(r["remind_at"])
                time_str = remind_at.strftime("%H:%M")
                fired = bool(r["fired"]) or remind_at <= now
                mark = "✅" if fired else "⏳"
                lines.append(f"- {time_str} {r['message']} {mark}")
            except (ValueError, TypeError):
                pass

        return lines if lines else None
