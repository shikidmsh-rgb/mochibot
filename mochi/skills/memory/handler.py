"""Memory skill — save, recall, and manage user memories via tool calls."""

import json
import logging

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import (
    save_memory_item, recall_memory as db_recall,
    get_core_memory, update_core_memory,
    list_all_memories as db_list_all, delete_memory_items,
    get_memory_stats as db_stats,
    list_memory_trash as db_list_trash,
    restore_memory_from_trash as db_restore_trash,
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
            # Generate embedding for hybrid vector search
            query_embedding = None
            if query:
                try:
                    from mochi.model_pool import get_pool
                    query_embedding = get_pool().embed(query)
                except Exception:
                    pass  # fall back to keyword-only search
            try:
                items = db_recall(uid, query=query, category=category,
                                  query_embedding=query_embedding)
            except Exception as e:
                log.error("recall_memory failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed to recall memories: {e}", success=False)
            if not items:
                return SkillResult(output="No matching memories found.")
            lines = [
                f"- #{m['id']} [{m['category']}] ★{m['importance']} | {m['content']}"
                for m in items[:15]
            ]
            return SkillResult(output=f"Found {len(items)} memories:\n" + "\n".join(lines))

        elif tool == "update_core_memory":
            action = args.get("action", "add")
            content = args.get("content", "")
            if not content:
                return SkillResult(output="Empty content.", success=False)
            current = get_core_memory(uid) or ""

            if action == "add":
                new_line = f"- {content}"
                updated = (current.rstrip() + "\n" + new_line) if current.strip() else new_line
                try:
                    update_core_memory(uid, updated)
                except Exception as e:
                    log.error("update_core_memory add failed: %s", e, exc_info=True)
                    return SkillResult(output=f"Failed: {e}", success=False)
                return SkillResult(output=f"Core memory: added → {content}")

            elif action == "delete":
                lines = current.split("\n")
                keyword = content.lower()
                remaining = [l for l in lines if keyword not in l.lower()]
                if len(remaining) == len(lines):
                    return SkillResult(output=f"Core memory: no line matching '{content}' found.")
                removed_count = len(lines) - len(remaining)
                updated = "\n".join(remaining)
                try:
                    update_core_memory(uid, updated)
                except Exception as e:
                    log.error("update_core_memory delete failed: %s", e, exc_info=True)
                    return SkillResult(output=f"Failed: {e}", success=False)
                return SkillResult(output=f"Core memory: deleted {removed_count} line(s) matching '{content}'.")

            return SkillResult(output=f"Unknown action: {action}. Use 'add' or 'delete'.", success=False)

        elif tool == "list_memories":
            category = args.get("category", "")
            limit = args.get("limit", 30)
            try:
                items = db_list_all(uid, category=category, limit=limit)
            except Exception as e:
                log.error("list_memories failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed: {e}", success=False)
            if not items:
                return SkillResult(output="No memories found.")
            lines = [
                f"#{m['id']} [{m['category']}] ★{m['importance']} | {m['content']} "
                f"(updated {m['updated_at'][:10]})"
                for m in items
            ]
            return SkillResult(output="\n".join(lines))

        elif tool == "delete_memory":
            mid = args.get("memory_id")
            if not mid:
                return SkillResult(output="Need memory_id.", success=False)
            count = delete_memory_items([mid], deleted_by="user")
            if count > 0:
                return SkillResult(
                    output=f"Memory #{mid} moved to trash (kept 30 days, restorable). "
                           "Use memory_trash_bin to recover if needed."
                )
            return SkillResult(output=f"Memory #{mid} not found.", success=False)

        elif tool == "memory_stats":
            try:
                stats = db_stats(uid)
                trash = db_list_trash(uid, limit=100)
            except Exception as e:
                log.error("memory_stats failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed: {e}", success=False)
            lines = [
                "Memory Stats:",
                f"- Total memories: {stats['total']}",
                f"- Critical (★3): {stats['high_importance']}",
                f"- Categories: {json.dumps(stats['categories'], ensure_ascii=False)}",
                f"- Trash bin: {len(trash)} items",
            ]
            return SkillResult(output="\n".join(lines))

        elif tool == "view_core_memory":
            core = get_core_memory(uid)
            if not core:
                return SkillResult(output="Core memory is empty.")
            return SkillResult(output=f"Core Memory:\n{core}")

        elif tool == "memory_trash_bin":
            action = args.get("action", "list")
            if action == "list":
                try:
                    trash = db_list_trash(uid)
                except Exception as e:
                    log.error("memory_trash_bin list failed: %s", e, exc_info=True)
                    return SkillResult(output=f"Failed: {e}", success=False)
                if not trash:
                    return SkillResult(output="Trash is empty.")
                lines = ["Deleted memories (kept 30 days):"]
                for t in trash:
                    lines.append(
                        f"Trash#{t['id']} (was #{t['original_id']}) [{t['category']}] "
                        f"★{t['importance']} | {t['content']} "
                        f"(deleted {t['deleted_at'][:10]} by {t['deleted_by']})"
                    )
                return SkillResult(output="\n".join(lines))

            elif action == "restore":
                tid = args.get("trash_id")
                if not tid:
                    return SkillResult(
                        output="Need trash_id to restore. Use memory_trash_bin(action='list') first.",
                        success=False,
                    )
                new_id = db_restore_trash(tid, uid)
                if new_id:
                    return SkillResult(output=f"Restored from trash! New memory #{new_id}.")
                return SkillResult(output=f"Trash item #{tid} not found.", success=False)

            return SkillResult(output=f"Unknown action: {action}", success=False)

        return SkillResult(output=f"Unknown tool: {tool}", success=False)
