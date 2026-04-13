"""Todo skill handler — execute logic only. Tool defs in SKILL.md."""

from datetime import datetime

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import create_todo, get_todos, complete_todo, delete_todo, update_todo


class TodoSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        uid = context.user_id

        if not action:
            return SkillResult(
                output="Error: 'action' is required. Valid actions: add, list, complete, delete, update.",
                success=False)

        if action == "add":
            task = args.get("task", "")
            if not task:
                return SkillResult(output="Error: 'task' is required for add.", success=False)
            nudge_date = args.get("nudge_date")
            tid = create_todo(uid, task, nudge_date=nudge_date)
            nudge_str = f" (📅 {nudge_date} 提醒)" if nudge_date else ""
            return SkillResult(output=f"Todo #{tid} added: '{task}'.{nudge_str}")

        elif action == "list":
            todos = get_todos(uid, include_done=args.get("include_done", False))
            if not todos:
                return SkillResult(output="No todos found.")
            lines = []
            for t in todos:
                mark = "✅" if t["done"] else "⬜"
                nudge = f" 📅{t['nudge_date']}" if t.get("nudge_date") else ""
                lines.append(f"#{t['id']} {mark} {t['task']}{nudge}")
            return SkillResult(output="\n".join(lines))

        elif action == "complete":
            todo_id = args.get("todo_id")
            if not todo_id:
                return SkillResult(output="Error: 'todo_id' is required for complete.", success=False)
            ok = complete_todo(uid, int(todo_id))
            return SkillResult(
                output=f"Todo #{todo_id} completed!" if ok else f"Todo #{todo_id} not found.")

        elif action == "delete":
            todo_id = args.get("todo_id")
            if not todo_id:
                return SkillResult(output="Error: 'todo_id' is required for delete.", success=False)
            ok = delete_todo(uid, int(todo_id))
            return SkillResult(
                output=f"Todo #{todo_id} deleted." if ok else f"Todo #{todo_id} not found.")

        elif action == "update":
            todo_id = args.get("todo_id")
            if not todo_id:
                return SkillResult(output="Error: 'todo_id' is required for update.", success=False)
            fields = {}
            for key in ("task", "nudge_date"):
                if key in args:
                    fields[key] = args[key]
            if not fields:
                return SkillResult(
                    output="Error: provide at least one field to update (task, nudge_date).",
                    success=False)
            ok = update_todo(uid, int(todo_id), **fields)
            parts = ", ".join(f"{k}={v}" for k, v in fields.items())
            return SkillResult(
                output=f"Todo #{todo_id} updated: {parts}." if ok else f"Todo #{todo_id} not found.")

        return SkillResult(output=f"Unknown todo action: {action}", success=False)

    # ── Diary integration ─────────────────────────────────────

    def diary_status(self, user_id: int, today: str, now: datetime) -> list[str] | None:
        from mochi.skills.todo.queries import get_visible_todos

        todos = get_visible_todos(today)
        if not todos:
            return None

        lines: list[str] = []
        for t in todos:
            overdue = t.get("nudge_date") and t["nudge_date"] < today
            tag = " ⚠️逾期" if overdue else ""
            lines.append(f"- [ ] {t['task']} [todo_id={t['id']}]{tag}")
        return lines if lines else None
