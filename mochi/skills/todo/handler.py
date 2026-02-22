"""Todo skill — manage a simple todo list via unified tool."""

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import create_todo, get_todos, complete_todo, delete_todo


class TodoSkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        args = context.args
        action = args.get("action", "list")
        uid = context.user_id

        if action == "add":
            task = args.get("task", "")
            if not task:
                return SkillResult(output="Need a task description.", success=False)
            category = args.get("category", "")
            tid = create_todo(uid, task, category)
            return SkillResult(output=f"Todo #{tid} added: {task}")

        elif action == "list":
            todos = get_todos(uid)
            if not todos:
                return SkillResult(output="No pending todos. All clear!")
            lines = [f"- {'✅' if t['done'] else '⬜'} #{t['id']} {t['task']}"
                     for t in todos]
            return SkillResult(output=f"{len(todos)} todos:\n" + "\n".join(lines))

        elif action == "complete":
            tid = args.get("todo_id")
            if not tid:
                return SkillResult(output="Need todo_id.", success=False)
            complete_todo(int(tid))
            return SkillResult(output=f"Todo #{tid} completed!")

        elif action == "delete":
            tid = args.get("todo_id")
            if not tid:
                return SkillResult(output="Need todo_id.", success=False)
            delete_todo(int(tid))
            return SkillResult(output=f"Todo #{tid} deleted.")

        return SkillResult(output=f"Unknown action: {action}", success=False)
