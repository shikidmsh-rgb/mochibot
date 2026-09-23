"""Todo skill handler — execute logic only. Tool defs in SKILL.md."""

from datetime import datetime

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.todo.queries import (
    create_todo, get_todos, delete_todo, mutate_todo,
    find_todos_by_exact_match, set_todo_done,
)


class TodoSkill(Skill):

    def get_tools(self) -> list[dict]:
        tools = super().get_tools()
        for tool in tools:
            tool["function"]["parameters"]["additionalProperties"] = False
        return tools

    def init_schema(self, conn) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS todos (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                task       TEXT    NOT NULL,
                done       INTEGER NOT NULL DEFAULT 0,
                category   TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_user
                ON todos(user_id, done);
        """)
        from mochi.db import ensure_column
        ensure_column(conn, "todos", "nudge_date", "TEXT DEFAULT NULL")
        ensure_column(conn, "todos", "source", "TEXT DEFAULT ''")
        ensure_column(conn, "todos", "completed_at", "TEXT DEFAULT NULL")

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action")
        uid = context.user_id

        if not action:
            return SkillResult(
                output="Error: 'action' is required. Valid actions: add, list, complete, reopen, update, delete.",
                success=False)

        if action == "add":
            task = args.get("task", "").strip()
            if not task:
                return SkillResult(output="Error: 'task' is required for add.", success=False)
            nudge_date = args.get("nudge_date")
            if nudge_date is not None:
                error = self._validate_nudge_date(nudge_date)
                if error:
                    return error
            tid = create_todo(uid, task, nudge_date=nudge_date)
            nudge_str = f" (📅 {nudge_date} 提醒)" if nudge_date else ""
            receipt = f"Todo #{tid} added: '{task}'.{nudge_str}"
            return SkillResult(
                output=receipt, summary=receipt,
                entity_refs=[f"todo:{tid}"], state_changed=True,
            )

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

        elif action in {"complete", "reopen"}:
            todo_id = self._resolve_todo(uid, args, action, done=(action == "reopen"))
            if isinstance(todo_id, SkillResult):
                return todo_id
            status = set_todo_done(uid, todo_id, action == "complete")
            if status == "unchanged":
                return SkillResult(
                    output=f"Todo #{todo_id} was already completed." if action == "complete"
                    else f"Todo #{todo_id} was already active.",
                    entity_refs=[f"todo:{todo_id}"],
                )
            ok = status == "updated"
            receipt = f"Todo #{todo_id} not found."
            if ok:
                receipt = f"Todo #{todo_id} completed!" if action == "complete" else f"Todo #{todo_id} reopened."
            return SkillResult(
                output=receipt, success=ok, summary=receipt if ok else "",
                entity_refs=[f"todo:{todo_id}"] if ok else [], state_changed=ok,
            )

        elif action == "delete":
            todo_id = args.get("todo_id")
            if not todo_id:
                return SkillResult(output="Error: 'todo_id' is required for delete.", success=False)
            if not self._valid_id(todo_id):
                return SkillResult(output="Error: todo_id must be a positive integer.", success=False)
            ok = delete_todo(uid, todo_id)
            receipt = f"Todo #{todo_id} deleted." if ok else f"Todo #{todo_id} not found."
            return SkillResult(
                output=receipt, success=ok, summary=receipt if ok else "",
                entity_refs=[f"todo:{todo_id}"] if ok else [], state_changed=ok,
            )

        elif action == "update":
            todo_id = self._resolve_todo(uid, args, action, done=None)
            if isinstance(todo_id, SkillResult):
                return todo_id
            fields = {}
            if "task" in args:
                fields["task"] = args["task"].strip()
                if not fields["task"]:
                    return SkillResult(output="Error: task cannot be empty.", success=False)
            if args.get("clear_nudge_date") is True:
                if "nudge_date" in args:
                    return SkillResult(
                        output="Error: use either nudge_date or clear_nudge_date, not both.",
                        success=False,
                    )
                fields["nudge_date"] = None
            elif "nudge_date" in args:
                error = self._validate_nudge_date(args["nudge_date"])
                if error:
                    return error
                fields["nudge_date"] = args["nudge_date"]
            if not fields:
                return SkillResult(
                    output="Error: provide task, nudge_date, or clear_nudge_date=true.",
                    success=False)
            status = mutate_todo(uid, todo_id, **fields)
            if status == "not_found":
                return SkillResult(
                    output=f"Todo #{todo_id} not found.",
                    success=False,
                )
            if status == "unchanged":
                return SkillResult(output=f"Todo #{todo_id} was already unchanged.")
            ok = status == "updated"
            parts = ", ".join(f"{k}={v}" for k, v in fields.items())
            receipt = (
                f"Todo #{todo_id} updated: {parts}." if ok
                else f"Todo #{todo_id} not found."
            )
            return SkillResult(
                output=receipt, success=ok, summary=receipt if ok else "",
                entity_refs=[f"todo:{todo_id}"] if ok else [], state_changed=ok,
            )

        return SkillResult(output=f"Unknown todo action: {action}", success=False)

    @staticmethod
    def _valid_id(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _resolve_todo(self, user_id: int, args: dict, action: str, *, done):
        if "todo_id" in args:
            if not self._valid_id(args["todo_id"]):
                return SkillResult(output="Error: todo_id must be a positive integer.", success=False)
            return args["todo_id"]
        match = (args.get("match") or "").strip()
        if not match:
            return SkillResult(
                output=f"Error: todo_id or match is required for {action}.", success=False,
            )
        matches = find_todos_by_exact_match(user_id, match, done=done)
        if len(matches) == 1:
            return matches[0]["id"]
        candidates = matches or [
            todo for todo in get_todos(user_id, include_done=True)
            if done is None or todo["done"] is done
        ]
        lines = [
            f"#{todo['id']} {'✅' if todo['done'] else '⬜'} {todo['task']}"
            for todo in candidates[:10]
        ]
        reason = "Multiple exact matches" if matches else "No exact match"
        candidate_text = "\n".join(lines) if lines else "(none)"
        return SkillResult(
            output=f"{reason} for {match!r}; no todo was changed.\nCandidates:\n{candidate_text}",
            success=False,
        )

    @staticmethod
    def _validate_nudge_date(value) -> SkillResult | None:
        try:
            if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") == value:
                return None
        except (ValueError, TypeError):
            pass
        return SkillResult(output="Error: nudge_date must use YYYY-MM-DD.", success=False)

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
