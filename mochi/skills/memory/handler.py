"""Memory skill — save, recall, and manage user memories via tool calls."""

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import (
    save_memory_item, recall_memory as db_recall,
    get_core_memory, update_core_memory,
)


class MemorySkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args
        uid = context.user_id

        if tool == "save_memory":
            content = args.get("content", "")
            category = args.get("category", "general")
            if not content:
                return SkillResult(output="Nothing to save.", success=False)
            mid = save_memory_item(uid, category=category, content=content)
            return SkillResult(output=f"Saved memory #{mid}: {content[:50]}")

        elif tool == "recall_memory":
            query = args.get("query", "")
            category = args.get("category", "")
            items = db_recall(uid, query=query, category=category)
            if not items:
                return SkillResult(output="No matching memories found.")
            lines = [f"- [{m['category']}] {m['content']}" for m in items[:15]]
            return SkillResult(output=f"Found {len(items)} memories:\n" + "\n".join(lines))

        elif tool == "update_core_memory":
            content = args.get("content", "")
            if not content:
                return SkillResult(output="Empty core memory update.", success=False)
            update_core_memory(uid, content)
            return SkillResult(output="Core memory updated.")

        return SkillResult(output=f"Unknown tool: {tool}", success=False)
