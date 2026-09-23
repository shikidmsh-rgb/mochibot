"""Local personal history, separate from automatic contextual recall."""

import asyncio
from copy import deepcopy
import logging
import re
import sqlite3

from mochi.skills.base import Skill, SkillContext, SkillResult
from mochi.skills.internal_search.queries import search_diary_entries

log = logging.getLogger(__name__)
_SOURCES = frozenset({"all", "conversation", "diary", "memory"})
_EXCERPT_CHARS = 280


def _excerpt(content: str, query: str) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= _EXCERPT_CHARS:
        return compact
    match_at = compact.casefold().find(query.casefold())
    start = max(0, match_at - _EXCERPT_CHARS // 3) if match_at >= 0 else 0
    end = min(len(compact), start + _EXCERPT_CHARS)
    if end == len(compact):
        start = max(0, end - _EXCERPT_CHARS)
    return (
        ("…" if start else "")
        + compact[start:end].strip()
        + ("…" if end < len(compact) else "")
    )


def _invalid(message: str) -> SkillResult:
    return SkillResult(
        output=message, success=False,
        error_code="invalid_arguments", retryable=True,
    )


class InternalSearchSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            definition["function"]["parameters"]["additionalProperties"] = False
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.tool_name != "search_personal_history":
            return SkillResult(
                output=f"Unknown tool: {context.tool_name}",
                success=False, error_code="unknown_tool", retryable=False,
            )
        query = context.args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _invalid("query is required.")
        query = query.strip()
        if len(query) > 200:
            return _invalid("query must be 200 characters or fewer.")
        source = context.args.get("source", "all")
        if not isinstance(source, str) or source not in _SOURCES:
            return _invalid("source must be all, conversation, diary, or memory.")
        limit = context.args.get("limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, int):
            return _invalid("limit must be an integer.")
        if not 1 <= limit <= 10:
            return _invalid("limit must be between 1 and 10.")
        if source in {"all", "conversation"} and context.source == "chat" and not context.turn_id:
            return SkillResult(
                output="Current conversation search context is unavailable. Try again next turn.",
                success=False, error_code="search_context_unavailable",
                retryable=True,
            )

        try:
            sections, notices, memory_ids = await asyncio.to_thread(
                self._search, context.user_id, context.turn_id, query, source, limit,
            )
        except (OSError, sqlite3.Error, ValueError):
            log.exception("Internal history search failed")
            return SkillResult(
                output="Internal search failed; local records were not changed.",
                success=False, error_code="local_search_failed", retryable=True,
            )

        matched = sum(len(items) for _, items in sections)
        lines = [
            f'Local matches for "{query}":'
            if matched else f'No local matches found for "{query}".'
        ]
        for label, items in sections:
            if items:
                lines.extend(["", f"{label} ({len(items)}):", *items])
        lines.extend(notices)
        return SkillResult(
            output="\n".join(lines),
            summary=f"Searched local personal history; {matched} match(es).",
            content_source="local_history",
            exposed_memory_ids=memory_ids,
        )

    @staticmethod
    def _search(
        user_id: int, turn_id: str, query: str, source: str, limit: int,
    ) -> tuple[list[tuple[str, list[str]]], list[str], list[int]]:
        from mochi.db import recall_memory, search_conversation_messages

        sections: list[tuple[str, list[str]]] = []
        notices: list[str] = []
        memory_ids: list[int] = []
        if source in {"all", "conversation"}:
            messages = search_conversation_messages(
                user_id, query, limit=limit, exclude_turn_id=turn_id or None,
            )
            sections.append(("Conversation", [
                f"- {item['created_at']} [{item['role']}]: {_excerpt(item['content'], query)}"
                for item in messages
            ]))
        if source in {"all", "diary"}:
            entries, truncated = search_diary_entries(query, limit)
            sections.append(("Diary", [
                f"- {item['date']}: {_excerpt(item['content'], query)}"
                for item in entries
            ]))
            if truncated:
                notices.append(
                    "Diary scan reached its local safety limit; "
                    "some saved entries were not checked."
                )
        if source in {"all", "memory"}:
            items = recall_memory(
                user_id, query=query, limit=limit,
                query_embedding=None, bump_access=False,
            )
            lines = []
            for item in items:
                start, end = item.get("evidence_start"), item.get("evidence_end")
                if start:
                    dates = f"{start}–{end}" if end and start != end else start
                    label = f"evidence {dates}"
                else:
                    label = f"record updated {item['updated_at']}"
                lines.append(
                    f"- #{item['id']} ({label}): {_excerpt(item['content'], query)}"
                )
                memory_ids.append(item["id"])
            sections.append(("Memory", lines))
        return sections, notices, memory_ids
