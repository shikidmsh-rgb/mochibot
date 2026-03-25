"""Memory skill — save, recall, and manage user memories via tool calls."""

import logging

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import (
    save_memory_item, recall_memory as db_recall,
    get_core_memory, update_core_memory,
)

log = logging.getLogger(__name__)


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
            try:
                mid = save_memory_item(uid, category=category, content=content)
            except Exception as e:
                log.error("save_memory failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed to save memory: {e}", success=False)
            return SkillResult(output=f"Saved memory #{mid}: {content[:50]}")

        elif tool == "recall_memory":
            query = args.get("query", "")
            category = args.get("category", "")
            try:
                items = db_recall(uid, query=query, category=category)
            except Exception as e:
                log.error("recall_memory failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed to recall memories: {e}", success=False)
            if not items:
                return SkillResult(output="No matching memories found.")
            lines = [f"- [{m['category']}] {m['content']}" for m in items[:15]]
            return SkillResult(output=f"Found {len(items)} memories:\n" + "\n".join(lines))

        elif tool == "update_core_memory":
            content = args.get("content", "")
            if not content:
                return SkillResult(output="Empty core memory update.", success=False)
            try:
                update_core_memory(uid, content)
            except Exception as e:
                log.error("update_core_memory failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed to update core memory: {e}", success=False)
            return SkillResult(output="Core memory updated.")

        return SkillResult(output=f"Unknown tool: {tool}", success=False)
