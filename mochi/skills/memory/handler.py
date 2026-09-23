"""Memory skill — update Core and manage background-extracted memories."""

import logging

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.db import (
    recall_memory as db_recall,
    list_all_memories as db_list_all, delete_memory_items,
    get_memory_stats as db_stats,
    list_memory_trash as db_list_trash,
    restore_memory_from_trash as db_restore_trash,
)
from mochi.core_store import CoreError, read_core, replace_core_exact

log = logging.getLogger(__name__)


class MemorySkill(Skill):

    async def execute(self, context: SkillContext) -> SkillResult:
        tool = context.tool_name
        args = context.args
        uid = context.user_id

        if tool == "recall_memory":
            query = args.get("query", "")
            # Generate embedding for hybrid vector search
            query_embedding = None
            if query:
                try:
                    from mochi.model_pool import get_pool
                    query_embedding = get_pool().embed(query)
                except Exception:
                    pass  # fall back to keyword-only search
            try:
                items = db_recall(
                    uid, query=query, limit=15, query_embedding=query_embedding,
                )
            except Exception as e:
                log.error("recall_memory failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed to recall memories: {e}", success=False)
            if not items:
                return SkillResult(output="No matching memories found.")
            lines = [
                f"- #{m['id']} ★{m['importance']} | {m['content']}"
                for m in items[:15]
            ]
            return SkillResult(output=f"Found {len(items)} memories:\n" + "\n".join(lines))

        elif tool == "update_core":
            expected_content = args.get("_expected_content")
            if not isinstance(expected_content, str):
                return SkillResult(
                    output="Core update context is unavailable. Try again next turn.",
                    success=False,
                )
            try:
                result = replace_core_exact(
                    expected_content=expected_content,
                    content=args.get("content", ""),
                    source="main",
                )
            except CoreError as e:
                current = read_core()
                return SkillResult(
                    output=(
                        f"Core update rejected: {e}\n\n"
                        f"Current Core:\n{current}"
                    ),
                    success=False,
                    document_snapshot=current,
                )
            receipt = (
                f"Core {'updated' if result['changed'] else 'unchanged'} "
                f"({result['tokens']}/{result['max_tokens']} estimated tokens)."
            )
            return SkillResult(
                output=f"{receipt}\n\nCurrent Core:\n{result['content']}",
                summary=receipt,
                entity_refs=["core"],
                state_changed=result["changed"],
                document_snapshot=result["content"],
            )

        elif tool == "list_memories":
            limit = args.get("limit", 30)
            try:
                items = db_list_all(uid, limit=limit)
            except Exception as e:
                log.error("list_memories failed: %s", e, exc_info=True)
                return SkillResult(output=f"Failed: {e}", success=False)
            if not items:
                return SkillResult(output="No memories found.")
            lines = [
                f"#{m['id']} ★{m['importance']} | {m['content']} "
                f"(evidence {_evidence_label(m)})"
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
                           "Use memory_trash_bin to recover if needed.",
                    state_changed=True,
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
                f"- 关键 (★3): {stats['high_importance']}",
                f"- Trash bin: {len(trash)} items",
            ]
            try:
                from mochi.knowledge_graph import get_kg_stats
                kg = get_kg_stats(uid)
                lines.append(
                    f"- KG: {kg['entities']} entities, "
                    f"{kg['active_triples']} active triples"
                )
            except Exception:
                pass
            return SkillResult(output="\n".join(lines))

        elif tool == "view_core_memory":
            core = read_core()
            if not core:
                return SkillResult(output="Core is empty.", document_snapshot=core)
            return SkillResult(output=f"Core:\n{core}", document_snapshot=core)

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
                        f"Trash#{t['id']} (was #{t['original_id']}) "
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
                    return SkillResult(
                        output=f"Restored from trash! New memory #{new_id}.",
                        state_changed=True,
                    )
                return SkillResult(output=f"Trash item #{tid} not found.", success=False)

            return SkillResult(output=f"Unknown action: {action}", success=False)

        return SkillResult(output=f"Unknown tool: {tool}", success=False)


def _evidence_label(item: dict) -> str:
    start = item.get("evidence_start", "")
    end = item.get("evidence_end", "")
    if start and end and start != end:
        return f"{start} to {end}"
    return start or "unknown"
