"""Bounded context and entry-scoped tools for silent Weekly Main."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from mochi import config
from mochi.db import (
    get_memory_items_by_ids,
    get_recent_user_messages_in_window,
)
from mochi.core_store import (
    CoreError,
    has_weekly_core_update,
    read_core,
    replace_weekly_core_exact,
)
from mochi.diary import DiaryArchiveWindow, read_diary_archive_window
from mochi.memory_curation import (
    MemoryCurationError,
    WeeklyMemoryCandidate,
    WeeklyMemoryCandidatePackage,
    build_weekly_memory_candidate_package,
    curate_memory_items,
)
from mochi.knowledge_graph import (
    ALLOWED_ENTITY_TYPES,
    ALLOWED_PREDICATES,
    RelationshipCurationError,
    curate_relationships,
    list_active_relationships,
)
from mochi.skills.base import SkillResult
from mochi.memory_contract import MAX_EVIDENCE_MESSAGE_IDS, MAX_MEMORY_CONTENT_CHARS


CORE_TOOL = "update_weekly_core"
CURATE_TOOL = "curate_weekly_memory"
RELATIONSHIP_TOOL = "curate_relationships"

_CORE_DEFINITION = {
    "type": "function",
    "function": {
        "name": CORE_TOOL,
        "description": (
            "根据当前看到的完整 Core，提交整理后的完整文档。保留仍然重要的"
            "认识，合并重复或过时表达；每周最多成功一次。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "整理后的完整 Core 文本。",
                },
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
}

_CURATE_DEFINITION = {
    "type": "function",
    "function": {
        "name": CURATE_TOOL,
        "description": (
            "整理眼前这一周的 Memory Items。你只需提交想做的改变、相关 item "
            "ID 和支持判断的 message ID；框架会核对你看到的版本并原子提交。"
            "没有需要改变的内容时，operations 可以为空。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["create", "edit", "merge", "archive"],
                            },
                            "item_id": {
                                "type": "integer",
                                "description": "edit 或 archive 的 Memory Item ID。",
                            },
                            "keep_item_id": {
                                "type": "integer",
                                "description": "merge 后保留的 Memory Item ID。",
                            },
                            "remove_item_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "minItems": 1,
                                "description": "merge 后归档的 Memory Item IDs。",
                            },
                            "content": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": MAX_MEMORY_CONTENT_CHARS,
                                "description": "create、edit 或 merge 后的记忆内容。",
                            },
                            "importance": {"type": "integer", "enum": [1, 2, 3]},
                            "evidence_message_ids": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "maxItems": MAX_EVIDENCE_MESSAGE_IDS,
                                "description": "直接支持这项决定的可见用户消息 ID。",
                            },
                        },
                        "required": ["op", "evidence_message_ids"],
                        "anyOf": [
                            {
                                "properties": {"op": {"enum": ["create"]}},
                                "required": ["content", "importance"],
                            },
                            {
                                "properties": {"op": {"enum": ["edit"]}},
                                "required": ["item_id", "content", "importance"],
                            },
                            {
                                "properties": {"op": {"enum": ["merge"]}},
                                "required": [
                                    "keep_item_id", "remove_item_ids", "content",
                                    "importance",
                                ],
                            },
                            {
                                "properties": {"op": {"enum": ["archive"]}},
                                "required": ["item_id"],
                            },
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
}

_RELATIONSHIP_DEFINITION = {
    "type": "function",
    "function": {
        "name": RELATIONSHIP_TOOL,
        "description": (
            "整理用户与人物、宠物、地点之间值得长期保留的关系。每次新增或"
            "更新只需引用支持它的可见 Memory Item ID；归档只需引用可见关系 "
            "ID。框架会核对当时可见的版本、证据和范围并原子提交。没有变化时 "
            "operations 可以为空。如果本轮也有 Memory 整理能力，先调用 "
            "curate_weekly_memory（即使 operations 为空），再整理关系。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": ["upsert", "archive"]},
                            "subject": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": sorted(ALLOWED_ENTITY_TYPES),
                                    },
                                },
                                "required": ["name", "type"],
                                "additionalProperties": False,
                            },
                            "predicate": {
                                "type": "string",
                                "enum": sorted(ALLOWED_PREDICATES),
                            },
                            "object": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "type": {
                                        "type": "string",
                                        "enum": sorted(ALLOWED_ENTITY_TYPES),
                                    },
                                },
                                "required": ["name", "type"],
                                "additionalProperties": False,
                            },
                            "source_memory": {
                                "type": "object",
                                "properties": {
                                    "item_id": {"type": "integer"},
                                },
                                "required": ["item_id"],
                                "additionalProperties": False,
                            },
                            "triple_id": {"type": "integer"},
                        },
                        "required": ["op"],
                        "anyOf": [
                            {
                                "properties": {"op": {"enum": ["upsert"]}},
                                "required": [
                                    "subject", "predicate", "object", "source_memory",
                                ],
                            },
                            {
                                "properties": {"op": {"enum": ["archive"]}},
                                "required": ["triple_id"],
                            },
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class WeeklyMaintenanceContext:
    logical_date: str
    period_key: str
    window_start: datetime
    window_end: datetime
    diary: DiaryArchiveWindow
    package: WeeklyMemoryCandidatePackage
    recent_user_messages: tuple[dict, ...]
    active_relationships: tuple[dict, ...]
    allowed_item_ids: frozenset[int]
    allowed_evidence_message_ids: frozenset[int]
    rendered: str


def _render_candidate(item: WeeklyMemoryCandidate) -> str:
    lines = [
        (
            f"- item_id={item.id} importance={item.importance} source={item.source} "
            f"created_at={item.created_at} updated_at={item.updated_at}"
        ),
        f"  content: {item.content}",
    ]
    if item.evidence_excerpts:
        lines.append("  visible evidence:")
        lines.extend(
            (
                f"    - message_id={excerpt.message_id} "
                f"created_at={excerpt.created_at}: {excerpt.content}"
            )
            for excerpt in item.evidence_excerpts
        )
    else:
        lines.append("  visible evidence: none")
    return "\n".join(lines)


def _render_context(
    logical_date: str,
    diary: DiaryArchiveWindow,
    package: WeeklyMemoryCandidatePackage,
    recent_messages: list[dict],
    active_relationships: list[dict],
) -> str:
    window_items = (
        "\n".join(_render_candidate(item) for item in package.window_items)
        or "(none)"
    )
    related_items = (
        "\n".join(_render_candidate(item) for item in package.related_items)
        or "(none)"
    )
    recent = "\n".join(
        (
            f"- message_id={message['id']} created_at={message['created_at']}: "
            f"{message['content']}"
        )
        for message in recent_messages
    ) or "(none)"
    return (
        "## Weekly bounded context\n"
        f"logical_monday: {logical_date}\n"
        f"window: [{package.window_start}, {package.window_end})\n\n"
        "### Archived Diary (read-only)\n"
        f"available_dates: {', '.join(diary.dates) if diary.dates else 'none'}\n"
        f"total_chars: {diary.total_chars}\n"
        f"truncated: {str(diary.truncated).lower()}\n"
        f"{diary.content or '(none)'}\n\n"
        "### Window Memory candidates\n"
        f"window_total: {package.window_total}\n"
        f"rendered_count: {len(package.window_items)}\n"
        f"truncated: {str(package.window_truncated).lower()}\n"
        f"{window_items}\n\n"
        "### Related older Memory candidates\n"
        f"related_eligible_total: {package.related_eligible_total}\n"
        f"rendered_count: {len(package.related_items)}\n"
        f"truncated: {str(package.related_truncated).lower()}\n"
        f"{related_items}\n\n"
        "### Recent user messages available as additional evidence\n"
        f"{recent}\n\n"
        "### Active user-life relationships\n"
        f"{json.dumps(active_relationships, ensure_ascii=False)}\n\n"
        "Only rendered item and evidence IDs are in curation scope. "
        "Truncated or missing content was not reviewed. Core can inform judgment "
        "but cannot support a relationship upsert."
    )


def build_weekly_maintenance_context(
    *,
    user_id: int,
    logical_date: str,
    period_key: str,
) -> WeeklyMaintenanceContext:
    monday = date.fromisoformat(logical_date)
    if monday.weekday() != 0:
        raise ValueError("weekly logical date must be Monday")
    window_end = datetime.combine(monday, time.min, tzinfo=config.TZ)
    window_start = window_end - timedelta(days=7)
    diary = read_diary_archive_window(window_start.date(), window_end.date())
    package = build_weekly_memory_candidate_package(
        user_id, window_start, window_end,
    )
    recent_messages = get_recent_user_messages_in_window(
        user_id, window_start, window_end,
    )
    active_relationships = list_active_relationships(user_id)
    allowed_evidence = frozenset(
        set(package.allowed_evidence_message_ids)
        | {message["id"] for message in recent_messages}
    )
    return WeeklyMaintenanceContext(
        logical_date=logical_date,
        period_key=period_key,
        window_start=window_start,
        window_end=window_end,
        diary=diary,
        package=package,
        recent_user_messages=tuple(recent_messages),
        active_relationships=tuple(active_relationships),
        allowed_item_ids=package.allowed_item_ids,
        allowed_evidence_message_ids=allowed_evidence,
        rendered=_render_context(
            logical_date, diary, package, recent_messages,
            active_relationships,
        ),
    )


@dataclass
class WeeklyMaintenanceSession:
    user_id: int
    context: WeeklyMaintenanceContext
    expected_core: str = ""
    core_succeeded: bool = False
    curation_succeeded: bool = False
    relationships_succeeded: bool = False
    memory_snapshots: dict[int, dict] = field(default_factory=dict, init=False)
    relationship_snapshots: dict[int, dict] = field(default_factory=dict, init=False)
    _pending_relationship_context: tuple[dict, dict] | None = field(
        default=None, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        self.memory_snapshots = {
            item.id: {
                "item_id": item.id, "content": item.content,
                "updated_at": item.updated_at,
            }
            for item in (
                *self.context.package.window_items, *self.context.package.related_items,
            )
        }
        self.relationship_snapshots = {
            item["triple_id"]: dict(item) for item in self.context.active_relationships
        }

    def advance_visible_context(self) -> None:
        """Advance only when the next model round can see the previous results."""
        if self._pending_relationship_context is not None:
            self.memory_snapshots, self.relationship_snapshots = (
                self._pending_relationship_context
            )
            self._pending_relationship_context = None

    def _memory_snapshot(self, item_id: int) -> dict:
        if item_id not in self.memory_snapshots:
            raise MemoryCurationError(
                f"Memory item {item_id} is outside the visible Weekly scope."
            )
        return dict(self.memory_snapshots[item_id])

    def _memory_expected(self, item_id: int) -> dict:
        snapshot = self._memory_snapshot(item_id)
        return {
            "item_id": item_id, "expected_content": snapshot["content"],
            "expected_updated_at": snapshot["updated_at"],
        }

    def _memory_operations(self, operations: list[dict]) -> list[dict]:
        hydrated = []
        for raw in operations:
            operation = dict(raw)
            if operation["op"] in {"edit", "archive"}:
                operation.update(self._memory_expected(operation["item_id"]))
            elif operation["op"] == "merge":
                operation["keep"] = self._memory_expected(operation.pop("keep_item_id"))
                operation["remove"] = [
                    self._memory_expected(item_id)
                    for item_id in operation.pop("remove_item_ids")
                ]
            hydrated.append(operation)
        return hydrated

    def _relationship_operations(self, operations: list[dict]) -> list[dict]:
        hydrated = []
        for raw in operations:
            operation = dict(raw)
            if operation["op"] == "upsert":
                operation["source_memory"] = self._memory_snapshot(
                    operation["source_memory"]["item_id"],
                )
            elif operation["op"] == "archive":
                triple_id = operation.pop("triple_id")
                if triple_id not in self.relationship_snapshots:
                    raise RelationshipCurationError(
                        f"Relationship {triple_id} is outside the visible Weekly scope."
                    )
                operation["expected"] = dict(self.relationship_snapshots[triple_id])
            hydrated.append(operation)
        return hydrated

    def definitions(self) -> list[dict]:
        definitions = []
        if not self.core_succeeded:
            definitions.append(_CORE_DEFINITION)
        if (
            not self.curation_succeeded
            and (
                self.context.allowed_item_ids
                or self.context.allowed_evidence_message_ids
            )
        ):
            definitions.append(_CURATE_DEFINITION)
        if not self.relationships_succeeded:
            definitions.append(_RELATIONSHIP_DEFINITION)
        return definitions

    def owns(self, tool_name: str) -> bool:
        return tool_name in {CORE_TOOL, CURATE_TOOL, RELATIONSHIP_TOOL}

    async def execute(self, tool_name: str, args: dict) -> SkillResult:
        if tool_name == CORE_TOOL:
            return await self._update_core(args)
        if tool_name == CURATE_TOOL:
            return await self._curate(args)
        if tool_name == RELATIONSHIP_TOOL:
            return await self._curate_relationships(args)
        return SkillResult(output=f"Unknown Weekly tool: {tool_name}", success=False)

    async def _update_core(self, args: dict) -> SkillResult:
        if set(args) != {"content"}:
            return SkillResult(
                output="Weekly Core update accepts the revised complete document.",
                success=False,
            )
        if self.core_succeeded:
            return SkillResult(
                output="Weekly Core update already completed.",
                success=False,
            )
        try:
            outcome = await asyncio.to_thread(
                replace_weekly_core_exact,
                user_id=self.user_id,
                period_key=self.context.period_key,
                expected_content=self.expected_core,
                content=args["content"],
            )
        except CoreError as exc:
            return SkillResult(
                output=f"Weekly Core update rejected: {exc}",
                success=False,
            )
        if outcome == "conflict":
            current = await asyncio.to_thread(read_core)
            return SkillResult(
                output=(
                    "Weekly Core update rejected: Core changed after packaging.\n\n"
                    f"Current Core:\n{current}"
                ),
                success=False,
                document_snapshot=current,
            )
        self.core_succeeded = True
        if outcome == "replayed":
            return SkillResult(
                output="Weekly Core revision already committed for this week.",
                summary="Weekly Core revision replayed safely.",
            )
        return SkillResult(
            output="Weekly Core revision committed with a snapshot.",
            summary="Weekly Core revision committed.",
            state_changed=True,
        )

    async def _curate(self, args: dict) -> SkillResult:
        if set(args) != {"operations"}:
            return SkillResult(
                output="Weekly curation accepts only the operations array.",
                success=False,
            )
        if self.curation_succeeded:
            return SkillResult(
                output="Weekly curation already completed.",
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                curate_memory_items,
                self.user_id,
                self.context.allowed_item_ids,
                self.context.allowed_evidence_message_ids,
                self._memory_operations(args["operations"]),
                period_key=self.context.period_key,
            )
        except (MemoryCurationError, TypeError, KeyError) as exc:
            return SkillResult(
                output=f"Weekly curation rejected: {exc}",
                success=False,
            )
        self.curation_succeeded = True
        changed_ids = list(dict.fromkeys((
            *result.created_ids,
            *result.changed_ids,
            *result.archived_ids,
        )))
        current_item_ids = list(dict.fromkeys((
            *result.created_ids,
            *result.changed_ids,
        )))
        current_items = await asyncio.to_thread(
            get_memory_items_by_ids,
            self.user_id,
            current_item_ids,
        )
        next_memories = dict(self.memory_snapshots)
        for item_id in result.archived_ids:
            next_memories.pop(item_id, None)
        next_memories.update({
            item["id"]: {
                "item_id": item["id"], "content": item["content"],
                "updated_at": item["updated_at"],
            }
            for item in current_items
        })
        active_relationships = await asyncio.to_thread(
            list_active_relationships,
            self.user_id,
        )
        self._pending_relationship_context = (
            next_memories,
            {item["triple_id"]: dict(item) for item in active_relationships},
        )
        receipt_payload = {
            "status": "replayed" if result.replayed else "committed",
            "created_ids": list(result.created_ids),
            "changed_ids": list(result.changed_ids),
            "archived_ids": list(result.archived_ids),
            "relationship_context": {
                "memory_items": [
                    {
                        "item_id": item["id"],
                        "content": item["content"],
                        "updated_at": item["updated_at"],
                    }
                    for item in current_items
                ],
                "active_relationships": active_relationships,
            },
        }
        receipt = json.dumps(receipt_payload, ensure_ascii=False)
        return SkillResult(
            output=receipt,
            summary=(
                "Weekly Memory curation "
                f"{'replayed safely' if result.replayed else 'committed'}."
            ),
            entity_refs=[f"memory:{item_id}" for item_id in changed_ids],
            state_changed=bool(changed_ids) and not result.replayed,
        )

    async def _curate_relationships(self, args: dict) -> SkillResult:
        if set(args) != {"operations"}:
            return SkillResult(
                output="Relationship curation accepts only the operations array.",
                success=False,
            )
        if self.relationships_succeeded:
            return SkillResult(
                output="Weekly relationship curation already completed.",
                success=False,
            )
        memory_curation_available = bool(
            self.context.allowed_item_ids
            or self.context.allowed_evidence_message_ids
        )
        if memory_curation_available and not self.curation_succeeded:
            return SkillResult(
                output=(
                    "Weekly relationship curation waits for Memory curation so "
                    "its evidence and active-relationship snapshots are current."
                ),
                success=False,
            )
        try:
            result = await asyncio.to_thread(
                curate_relationships,
                self.user_id,
                set(self.memory_snapshots),
                self._relationship_operations(args["operations"]),
            )
        except (RelationshipCurationError, MemoryCurationError, TypeError, KeyError) as exc:
            return SkillResult(
                output=f"Weekly relationship curation rejected: {exc}",
                success=False,
            )
        self.relationships_succeeded = True
        receipt = (
            "Weekly relationship curation committed: "
            f"upserted={list(result.upserted_ids)}, "
            f"archived={list(result.archived_ids)}."
        )
        changed_ids = (*result.upserted_ids, *result.archived_ids)
        return SkillResult(
            output=receipt,
            summary=receipt,
            entity_refs=[
                f"relationship:{relationship_id}"
                for relationship_id in changed_ids
            ],
            state_changed=bool(changed_ids),
        )


def create_weekly_session(
    *,
    user_id: int,
    logical_date: str,
    period_key: str,
) -> WeeklyMaintenanceSession:
    return WeeklyMaintenanceSession(
        user_id=user_id,
        context=build_weekly_maintenance_context(
            user_id=user_id,
            logical_date=logical_date,
            period_key=period_key,
        ),
        core_succeeded=has_weekly_core_update(user_id, period_key),
    )
