"""Reminder skill — create, list, and delete reminders via unified tool."""

from datetime import datetime, date as date_type

from mochi.config import TZ
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.reminder.queries import create_reminder, get_pending_reminders, delete_reminder
from mochi.reminder_timer import notify_new_reminder


class ReminderSkill(Skill):

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                channel_id INTEGER NOT NULL DEFAULT 0,
                message    TEXT    NOT NULL,
                remind_at  TEXT    NOT NULL,
                fired      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_pending
                ON reminders(fired, remind_at);
        """)
        from mochi.db import ensure_column
        ensure_column(conn, "reminders", "recurrence", "TEXT DEFAULT NULL")

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action", "list")
        uid = context.user_id

        if action == "create":
            message = args.get("message", "")
            remind_at_raw = args.get("remind_at", "")
            if not message or not remind_at_raw:
                return SkillResult(output="Need both message and remind_at.", success=False)

            try:
                remind_at_dt = datetime.fromisoformat(remind_at_raw)
            except (ValueError, TypeError):
                return SkillResult(
                    output=f"Invalid remind_at format: {remind_at_raw!r}. "
                           "Use ISO 8601, e.g. 2026-04-20T14:30:00+08:00",
                    success=False,
                )

            if remind_at_dt.tzinfo is None:
                remind_at_dt = remind_at_dt.replace(tzinfo=TZ)

            remind_at = remind_at_dt.isoformat()
            rid = create_reminder(uid, context.channel_id, message, remind_at)
            notify_new_reminder()
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
                deleted = delete_reminder(int(rid))
            except (ValueError, TypeError):
                return SkillResult(output=f"Invalid reminder_id: {rid}", success=False)
            if not deleted:
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            notify_new_reminder()
            return SkillResult(output=f"Reminder #{rid} deleted.")

        return SkillResult(output=f"Unknown action: {action}", success=False)

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.db import _connect

        # Query unfired reminders for today (including future times)
        conn = _connect()
        rows = conn.execute(
            "SELECT message, remind_at, fired FROM reminders "
            "WHERE user_id = ? AND fired = 0 AND remind_at >= ? AND remind_at < ? "
            "ORDER BY remind_at",
            (user_id, today, today + "T99"),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        lines: list[str] = []
        for r in rows:
            try:
                remind_at = datetime.fromisoformat(r["remind_at"])
                if remind_at.tzinfo is None:
                    remind_at = remind_at.replace(tzinfo=TZ)
                time_str = remind_at.strftime("%H:%M")
                fired = bool(r["fired"]) or remind_at <= now
                mark = "✅" if fired else "⏳"
                lines.append(f"- {time_str} {r['message']} {mark}")
            except (ValueError, TypeError):
                pass

        return lines if lines else None
