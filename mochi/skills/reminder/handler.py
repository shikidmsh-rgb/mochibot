"""Reminder skill — create, list, and cancel notify/self reminders."""

from datetime import datetime

from mochi.config import TZ
from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.reminder.queries import (
    cancel_reminder,
    create_reminder,
    create_self_reminder,
    get_active_reminders,
    reminder_deadline,
    update_active_reminder,
)
from mochi.reminder_timer import notify_new_reminder


def _bounded_summary(value: str, limit: int = 120) -> str:
    summary = " ".join(value.split())
    return summary if len(summary) <= limit else summary[:limit - 3] + "..."


def _current_owner_main(context: SkillContext) -> bool:
    if context.actor != "main":
        return False
    if (
        isinstance(context.user_id, bool)
        or not isinstance(context.user_id, int)
        or context.user_id <= 0
    ):
        return False
    from mochi.config import OWNER_USER_ID
    return not OWNER_USER_ID or context.user_id == OWNER_USER_ID


class ReminderSkill(Skill):

    def get_tools(self) -> list[dict]:
        tools = super().get_tools()
        for tool in tools:
            tool["function"]["parameters"]["additionalProperties"] = False
        return tools

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
        ensure_column(
            conn, "reminders", "status", "TEXT NOT NULL DEFAULT 'pending'",
        )
        ensure_column(conn, "reminders", "kind", "TEXT NOT NULL DEFAULT 'notify'")
        ensure_column(conn, "reminders", "context", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "source", "TEXT NOT NULL DEFAULT 'owner'")
        ensure_column(conn, "reminders", "transport", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "claimed_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "lease_until", "TEXT DEFAULT NULL")
        ensure_column(
            conn, "reminders", "attempt_count", "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(conn, "reminders", "next_attempt_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "last_error", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "prepared_text", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "result_json", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "outcome", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "handled_at", "TEXT DEFAULT NULL")
        ensure_column(
            conn, "reminders", "delivery_cursor", "INTEGER NOT NULL DEFAULT 0",
        )
        ensure_column(
            conn, "reminders", "delivery_started_at", "TEXT DEFAULT NULL",
        )
        ensure_column(conn, "reminders", "delivered_at", "TEXT DEFAULT NULL")
        ensure_column(conn, "reminders", "cancelled_at", "TEXT DEFAULT NULL")
        conn.execute(
            "UPDATE reminders SET status = 'delivered' "
            "WHERE fired = 1 AND status = 'pending'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reminders_schedule "
            "ON reminders(kind, status, next_attempt_at, remind_at, lease_until)"
        )

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        if context.tool_name == "schedule_self_reminder":
            args = {**args, "action": "create", "kind": "self"}
        action = args.get("action", "list")
        uid = context.user_id

        if action == "create":
            kind = args.get("kind", "notify")
            if kind not in {"notify", "self"}:
                return SkillResult(
                    output=f"Invalid reminder kind: {kind!r}.",
                    success=False,
                )
            remind_at_raw = args.get("remind_at", "")
            if not remind_at_raw:
                return SkillResult(output="Need remind_at.", success=False)
            message = args.get("message", "")
            intent = args.get("intent", "")
            if kind == "notify":
                if "intent" in args:
                    return SkillResult(output="Notify reminders accept message, not intent.", success=False)
                if not isinstance(message, str) or not message.strip():
                    return SkillResult(
                        output="Notify reminders need message and remind_at.",
                        success=False,
                    )
                message = message.strip()
            else:
                if not _current_owner_main(context):
                    return SkillResult(
                        output="Self reminders can only be created by Main.",
                        success=False,
                    )
                if not isinstance(intent, str) or not intent.strip():
                    return SkillResult(
                        output="Self reminders need intent and remind_at.",
                        success=False,
                    )
                if isinstance(message, str) and message.strip():
                    return SkillResult(
                        output="Self reminders accept intent, not a prewritten message.",
                        success=False,
                    )
                intent = intent.strip()

            remind_at, error = self._normalize_remind_at(remind_at_raw)
            if error:
                return error
            recurrence, error = self._normalize_recurrence(args.get("recurrence", "one_time"))
            if error:
                return error
            expires_at = reminder_deadline(remind_at)
            if kind == "self":
                rid = create_self_reminder(
                    uid,
                    context.channel_id,
                    intent,
                    remind_at,
                    context.transport,
                    recurrence,
                )
                receipt = (
                    f"Self reminder #{rid} set for {remind_at}: "
                    f"{_bounded_summary(intent)}"
                )
            else:
                rid = create_reminder(
                    uid, context.channel_id, message, remind_at, recurrence,
                )
                receipt = f"Reminder #{rid} set for {remind_at}: {message}"
            if recurrence:
                receipt += f" (repeats {recurrence})"
            receipt += f" Expires at {expires_at.isoformat()}; no delivery after expiry."
            notify_new_reminder()
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"reminder:{rid}"], state_changed=True,
            )

        elif action == "list":
            reminders = get_active_reminders(uid)
            if not reminders:
                return SkillResult(output="No pending reminders.")
            lines = []
            for reminder in reminders:
                if reminder["kind"] == "self":
                    content = _bounded_summary(reminder.get("context") or "")
                    label = "Mochi 到时重新看看"
                else:
                    content = reminder["message"]
                    label = "提醒用户"
                recurrence_label = f"（{reminder['recurrence']}）" if reminder.get("recurrence") else ""
                lines.append(
                    f"- #{reminder['id']} [{reminder['remind_at']}] "
                    f"{label}：{content}{recurrence_label}"
                )
            return SkillResult(
                output=f"{len(reminders)} reminders:\n" + "\n".join(lines)
            )

        elif action == "update":
            rid = args.get("reminder_id")
            if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
                return SkillResult(output="Need a valid reminder_id to update.", success=False)
            if "kind" in args:
                return SkillResult(output="Reminder kind cannot be changed by update.", success=False)
            reminder = next((r for r in get_active_reminders(uid) if r["id"] == rid), None)
            if reminder is None:
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            is_self = reminder["kind"] == "self"
            if is_self and not _current_owner_main(context):
                return SkillResult(output="Self reminders can only be created by Main.", success=False)
            if is_self and "message" in args:
                return SkillResult(output="Self reminders accept intent, not a prewritten message.", success=False)
            if not is_self and "intent" in args:
                return SkillResult(output="Notify reminders accept message, not intent.", success=False)
            content_arg = "intent" if is_self else "message"
            fields = {}
            if "remind_at" in args:
                value, error = self._normalize_remind_at(args["remind_at"])
                if error:
                    return error
                fields["remind_at"] = value
            if content_arg in args:
                value = args[content_arg]
                if not isinstance(value, str) or not value.strip():
                    return SkillResult(
                        output=f"{content_arg} must be non-empty when provided.", success=False,
                    )
                fields["context" if is_self else "message"] = value.strip()
            if "recurrence" in args:
                value, error = self._normalize_recurrence(args["recurrence"])
                if error:
                    return error
                fields["recurrence"] = value
            if not fields:
                return SkillResult(
                    output=f"Update reminder #{rid} with remind_at, {content_arg}, or recurrence.",
                    success=False,
                )
            status, updated = update_active_reminder(rid, uid, **fields)
            if status == "not_found":
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            if status == "started":
                return SkillResult(
                    output=f"Reminder #{rid} has already started or expired and cannot be updated.",
                    success=False,
                )
            if status == "expired_time":
                return self._expired_time_error()
            if status == "unchanged":
                return SkillResult(
                    output=f"Reminder #{rid} was already unchanged.",
                    entity_refs=[f"reminder:{rid}"],
                )
            content = updated["context"] if is_self else updated["message"]
            receipt = (
                f"Reminder #{rid} updated for {updated['remind_at']}: "
                f"{_bounded_summary(content)}"
            )
            if updated["recurrence"]:
                receipt += f" (repeats {updated['recurrence']})"
            receipt += (
                f" Expires at {reminder_deadline(updated['remind_at']).isoformat()}; "
                "no delivery after expiry."
            )
            notify_new_reminder()
            return SkillResult(
                output=receipt, summary=receipt, entity_refs=[f"reminder:{rid}"],
                state_changed=True,
            )

        elif action == "delete":
            rid = args.get("reminder_id")
            if not rid:
                return SkillResult(output="Need reminder_id to delete.", success=False)
            if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
                return SkillResult(output=f"Invalid reminder_id: {rid}", success=False)
            deleted = cancel_reminder(rid, uid)
            if not deleted:
                if any(r["id"] == rid for r in get_active_reminders(uid)):
                    return SkillResult(
                        output=f"Reminder #{rid} is currently being processed and cannot be deleted.",
                        success=False,
                    )
                return SkillResult(output=f"Reminder #{rid} not found.", success=False)
            notify_new_reminder()
            receipt = f"Reminder #{rid} deleted."
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"reminder:{rid}"], state_changed=True,
            )

        return SkillResult(output=f"Unknown action: {action}", success=False)

    @staticmethod
    def _expired_time_error() -> SkillResult:
        return SkillResult(
            output="Reminder time is already at least five minutes past; "
                   "choose a current or future time.",
            success=False,
        )

    @classmethod
    def _normalize_remind_at(cls, raw) -> tuple[str | None, SkillResult | None]:
        try:
            value = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None, SkillResult(
                output=f"Invalid remind_at format: {raw!r}. "
                       "Use ISO 8601, e.g. 2026-04-20T14:30:00+08:00",
                success=False,
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=TZ)
        result = value.isoformat()
        if reminder_deadline(result) <= datetime.now(TZ):
            return None, cls._expired_time_error()
        return result, None

    @staticmethod
    def _normalize_recurrence(raw) -> tuple[str | None, SkillResult | None]:
        if raw == "one_time":
            return None, None
        if raw in ("daily", "weekdays", "weekly"):
            return raw, None
        return None, SkillResult(
            output=f"Invalid recurrence: {raw!r}. Use one_time, daily, weekdays, or weekly.",
            success=False,
        )

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.db import _connect

        # Query unfired reminders for today (including future times)
        conn = _connect()
        rows = conn.execute(
            "SELECT message, remind_at, fired FROM reminders "
            "WHERE user_id = ? AND kind = 'notify' "
            "AND status IN ('pending', 'running', 'ready') "
            "AND remind_at >= ? AND remind_at < ? "
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
