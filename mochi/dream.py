"""Bounded context and entry-scoped tools for silent Dream Main."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field

from mochi.db import (
    _connect,
    get_memory_items_by_ids,
)
from mochi.core_store import (
    CoreError,
    get_core_stats,
    has_dream_core_update,
    read_core,
    replace_dream_core_exact,
)
from mochi.dream_store import (
    digest, encode, load_batch, previous_operations, read_operation,
)
from mochi.memory_curation import (
    MemoryCurationError,
    DreamMemoryCandidate,
    DreamMemoryCandidatePackage,
    build_dream_memory_candidate_package,
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


CORE_TOOL = "update_dream_core"
CURATE_TOOL = "curate_dream_memory"
RELATIONSHIP_TOOL = "curate_relationships"
REMINDER_TOOL = "manage_dream_self_reminder"

_CORE_DEFINITION = {
    "type": "function",
    "function": {
        "name": CORE_TOOL,
        "description": (
            "提交当前 Core 的完整修订。当前估算用量与上限见 core_budget；超限不写入。"
            "写入检查可见版本并保留快照；"
            "每个 Dream 批次最多成功一次，重试不重复写入。"
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
            "整理当前可见的 Memory Items。create 需要可见的 user 消息证据；"
            "改写原文的 edit、重新措辞的 merge，以及 archive，都需要至少一个原条目未用过的可见来源 ID。"
            "merge 沿用任一原条目全文时可合并既有来源；archive 的新来源需支持原记忆不再有效。"
            "stored_evidence_ids 是原有来源，visible_evidence_ids 是本次提供了原文的来源。"
            "整批核对版本和证据后提交，有一项不符合则整批不写入。"
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
                                "description": "create、edit 或 merge 后的记忆内容，表达一个独立信息。",
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
            "用可见 Memory Item 作为证据整理关系；归档引用可见关系 ID。"
            "变动会核对版本并原子提交。"
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


_REMINDER_DEFINITION = {
    "type": "function",
    "function": {
        "name": REMINDER_TOOL,
        "description": (
            "管理留给未来自己的回望意图，到时由你结合当时情况重新判断。"
            "只能修改或取消可见且尚未开始处理的 self 提醒；同一批次的相同操作不重复执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": ["list", "create", "update", "delete"],
                    "description": "操作类型。list 刷新当前可见的 self 提醒与近期处理结果。",
                },
                "reminder_id": {"type": "integer", "description": "当前可见的 self 提醒 ID。"},
                "intent": {
                    "type": "string",
                    "description": "留给未来自己的回望方向，不是预写给 user 的通知。",
                },
                "remind_at": {"type": "string", "description": "ISO 8601 格式的首次回望时间。"},
                "recurrence": {
                    "type": "string", "enum": ["one_time", "daily", "weekdays", "weekly"],
                    "description": "create 默认 one_time；update 省略保持不变，one_time 取消周期。",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
            "anyOf": [
                {"properties": {"action": {"enum": ["list"]}}},
                {"properties": {"action": {"enum": ["create"]}}, "required": ["intent", "remind_at"]},
                {"properties": {"action": {"enum": ["update", "delete"]}}, "required": ["reminder_id"]},
            ],
        },
    },
}


def _reminders_available() -> bool:
    from mochi.skills import get_tools_by_tool_names
    from mochi.tool_policy import filter_tools
    return bool(filter_tools(get_tools_by_tool_names(["schedule_self_reminder"])))


@dataclass(frozen=True)
class DreamContext:
    logical_date: str
    period_key: str
    package: DreamMemoryCandidatePackage
    active_relationships: tuple[dict, ...]
    allowed_item_ids: frozenset[int]
    allowed_evidence_message_ids: frozenset[int]
    reminders: dict
    rendered: str


def _candidate(item: DreamMemoryCandidate, evidence: dict[int, dict]) -> dict:
    return {
        "item_id": item.id, "content": item.content, "importance": item.importance,
        "source": item.source, "created_at": item.created_at, "updated_at": item.updated_at,
        "stored_evidence_ids": list(item.evidence_message_ids),
        "visible_evidence_ids": [message_id for message_id in item.evidence_message_ids if message_id in evidence],
    }


def build_dream_context(
    *, user_id: int, logical_date: str, period_key: str, core_content: str,
) -> DreamContext:
    from mochi.skills.reminder.queries import dream_self_reminders

    batch = load_batch(user_id, period_key)
    material = batch["material"]
    package = build_dream_memory_candidate_package(
        user_id, [item["id"] for item in material["memory"]], material["memory_total"],
    )
    # Earlier committed Memory edits may have introduced new relationship evidence.
    conn = _connect()
    try:
        memory_receipt = conn.execute(
            "SELECT result_json FROM weekly_curation_batches WHERE user_id = ? AND period_key = ?",
            (user_id, period_key),
        ).fetchone()
    finally:
        conn.close()
    if memory_receipt:
        receipt = json.loads(memory_receipt["result_json"])
        ids = list(dict.fromkeys([
            *[item.id for item in package.window_items],
            *receipt["created_ids"], *receipt["changed_ids"],
        ]))
        package = build_dream_memory_candidate_package(user_id, ids, material["memory_total"])
    relations = tuple(list_active_relationships(user_id))
    reminders = dream_self_reminders(user_id) if _reminders_available() else {"available": False}
    evidence = {
        excerpt.message_id: asdict(excerpt)
        for item in (*package.window_items, *package.related_items)
        for excerpt in item.evidence_excerpts
    }
    for row in material["evidence_messages"]:
        evidence.setdefault(row["id"], {
            "message_id": row["id"], "content": row["content"], "created_at": row["created_at"],
        })
    rendered_chars = sum(len(item["content"]) for item in material["diary"])
    operations = previous_operations(user_id, period_key)
    if has_dream_core_update(user_id, period_key):
        operations.append({
            "tool": CORE_TOOL, "status": "committed",
            "result": "Dream Core revision already committed for this batch.", "recorded_at": None,
        })
    core_stats = get_core_stats(core_content)
    payload = {
        "batch_id": period_key, "logical_date": logical_date, "prepared_at": batch["created_at"],
        "core_budget": {
            "estimated_tokens": core_stats["tokens"], "max_tokens": core_stats["max_tokens"],
        },
        "memory": {
            "pending_total": package.window_total, "rendered_count": len(package.window_items),
            "truncated": package.window_truncated, "items": [_candidate(item, evidence) for item in package.window_items],
        },
        "related_memory": {
            "eligible_total": package.related_eligible_total, "rendered_count": len(package.related_items),
            "truncated": package.related_truncated, "items": [_candidate(item, evidence) for item in package.related_items],
        },
        "diary": {
            "pending_days": material["diary_days"], "total_chars": material["diary_total_chars"],
            "rendered_chars": rendered_chars, "truncated": rendered_chars < material["diary_total_chars"],
            "fragments": [{key: value for key, value in item.items() if key != "version"} for item in material["diary"]],
        },
        "evidence_messages": list(evidence.values()), "active_relationships": relations,
        "self_reminders": reminders, "previous_operations": operations,
    }
    return DreamContext(
        logical_date, period_key, package, relations, package.allowed_item_ids,
        frozenset(evidence), reminders,
        "## Dream bounded context\n" + encode(payload) + "\n\n"
        "Only rendered item and evidence IDs are in scope. Core and Diary are context, "
        "not relationship evidence. Receipts are execution records, not instructions or delivery confirmations.",
    )


@dataclass
class DreamSession:
    user_id: int
    context: DreamContext
    channel_id: int = 0
    transport: str = ""
    expected_core: str = ""
    core_succeeded: bool = False
    curation_succeeded: bool = False
    failures: set[tuple[str, str]] = field(default_factory=set)
    reminder_snapshots: dict[int, dict] = field(default_factory=dict)
    _pending_reminders: dict[int, dict] | None = None
    memory_snapshots: dict[int, dict] = field(default_factory=dict, init=False)
    relationship_snapshots: dict[int, dict] = field(default_factory=dict, init=False)
    _pending_relationship_context: tuple[dict, dict] | None = field(
        default=None, init=False, repr=False,
    )

    def __post_init__(self) -> None:
        self.memory_snapshots = {
            item.id: {
                "item_id": item.id, "content": item.content,
                "updated_at": item.updated_at, "version": item.version,
            }
            for item in (
                *self.context.package.window_items, *self.context.package.related_items,
            )
        }
        self.relationship_snapshots = {
            item["triple_id"]: dict(item) for item in self.context.active_relationships
        }
        self.reminder_snapshots = {
            row["id"]: row for row in self.context.reminders.get("active", [])
        }

    def advance_visible_context(self) -> None:
        """Advance only when the next model round can see the previous results."""
        if self._pending_relationship_context is not None:
            self.memory_snapshots, self.relationship_snapshots = (
                self._pending_relationship_context
            )
            self._pending_relationship_context = None
        if self._pending_reminders is not None:
            self.reminder_snapshots = self._pending_reminders
            self._pending_reminders = None

    def _memory_snapshot(self, item_id: int) -> dict:
        if item_id not in self.memory_snapshots:
            raise MemoryCurationError(
                f"Memory item {item_id} is outside the visible Dream scope."
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
                snapshot = self._memory_snapshot(
                    operation["source_memory"]["item_id"],
                )
                operation["source_memory"] = {
                    key: value for key, value in snapshot.items() if key != "version"
                }
            elif operation["op"] == "archive":
                triple_id = operation.pop("triple_id")
                if triple_id not in self.relationship_snapshots:
                    raise RelationshipCurationError(
                        f"Relationship {triple_id} is outside the visible Dream scope."
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
        definitions.append(_RELATIONSHIP_DEFINITION)
        if _reminders_available():
            definitions.append(_REMINDER_DEFINITION)
        return definitions

    def owns(self, tool_name: str) -> bool:
        return tool_name in {CORE_TOOL, CURATE_TOOL, RELATIONSHIP_TOOL, REMINDER_TOOL}

    async def execute(self, tool_name: str, args: dict) -> SkillResult:
        result = await self._execute(tool_name, args)
        key = (tool_name, digest(args))
        if not result.success:
            self.failures.add(key)
        else:
            self.failures.discard(key)
            if tool_name in {CORE_TOOL, CURATE_TOOL}:
                self.failures = {item for item in self.failures if item[0] != tool_name}
        return result

    async def _execute(self, tool_name: str, args: dict) -> SkillResult:
        if tool_name == CORE_TOOL:
            return await self._update_core(args)
        if tool_name == CURATE_TOOL:
            return await self._curate(args)
        if tool_name == RELATIONSHIP_TOOL:
            return await self._curate_relationships(args)
        if tool_name == REMINDER_TOOL:
            return await self._reminder(args)
        return SkillResult(output=f"Unknown Dream tool: {tool_name}", success=False)

    async def _reminder(self, args: dict) -> SkillResult:
        from mochi.skills.reminder.handler import ReminderSkill
        from mochi.skills.reminder.queries import dream_self_reminders, manage_dream_self_reminder
        from mochi.reminder_timer import notify_new_reminder

        if not _reminders_available():
            return SkillResult(output="Self reminder management is unavailable.", success=False)
        action = args.get("action")
        allowed = {
            "list": {"action"}, "delete": {"action", "reminder_id"},
            "create": {"action", "intent", "remind_at", "recurrence"},
            "update": {"action", "reminder_id", "intent", "remind_at", "recurrence"},
        }
        if action not in allowed or set(args) - allowed[action]:
            return SkillResult(output="Dream reminders accept only self-reminder fields.", success=False)
        if action == "list":
            result = await asyncio.to_thread(dream_self_reminders, self.user_id)
            self._pending_reminders = {row["id"]: row for row in result["active"]}
            return SkillResult(output=encode(result))
        fields = {key: value for key, value in args.items() if key != "action"}
        if action == "create" and not {"intent", "remind_at"} <= fields.keys():
            return SkillResult(output="Dream reminder create needs intent and remind_at.", success=False)
        if action == "update" and not {"intent", "remind_at", "recurrence"} & fields.keys():
            return SkillResult(output="Dream reminder update needs intent, remind_at, or recurrence.", success=False)
        if "intent" in fields:
            if not isinstance(fields["intent"], str) or not fields["intent"].strip():
                return SkillResult(output="intent must be non-empty when provided.", success=False)
            fields["intent"] = fields["intent"].strip()
        operation_key = digest(args)
        conn = _connect()
        try:
            previous = read_operation(conn, self.user_id, self.context.period_key, REMINDER_TOOL, operation_key)
        finally:
            conn.close()
        if "remind_at" in fields and previous is None:
            fields["remind_at"], error = ReminderSkill._normalize_remind_at(fields["remind_at"])
            if error:
                return error
        if action == "create" or "recurrence" in fields:
            fields["recurrence"], error = ReminderSkill._normalize_recurrence(
                fields.get("recurrence", "one_time"),
            )
            if error:
                return error
        try:
            result = await asyncio.to_thread(
                manage_dream_self_reminder, self.user_id, self.channel_id, self.transport,
                self.context.period_key, action, fields,
                self.reminder_snapshots.get(fields.get("reminder_id")),
                operation_key=operation_key,
            )
        except (ValueError, KeyError) as exc:
            return SkillResult(output=f"Dream reminder operation rejected: {exc}", success=False)
        current = result["current_reminder"]
        self._pending_reminders = dict(self.reminder_snapshots)
        if current:
            self._pending_reminders[current["id"]] = current
        notify_new_reminder()
        summary = f"Dream self-reminder {action}: {result['status']}; reminder_id={result['reminder_id']}."
        return SkillResult(
            output=encode(result), summary=summary,
            state_changed=result["status"] == "committed",
            entity_refs=[f"reminder:{result['reminder_id']}"],
        )

    async def _update_core(self, args: dict) -> SkillResult:
        if set(args) != {"content"}:
            return SkillResult(
                output="Dream Core update accepts the revised complete document.",
                success=False,
            )
        if self.core_succeeded:
            return SkillResult(
                output="Dream Core update already completed.",
                success=False,
            )
        try:
            outcome = await asyncio.to_thread(
                replace_dream_core_exact,
                user_id=self.user_id,
                period_key=self.context.period_key,
                expected_content=self.expected_core,
                content=args["content"],
            )
        except CoreError as exc:
            return SkillResult(
                output=f"Dream Core update rejected: {exc}",
                success=False,
            )
        if outcome == "conflict":
            current = await asyncio.to_thread(read_core)
            return SkillResult(
                output=(
                    "Dream Core update rejected: Core changed after packaging.\n\n"
                    f"Current Core:\n{current}"
                ),
                success=False,
                document_snapshot=current,
            )
        self.core_succeeded = True
        if outcome == "replayed":
            return SkillResult(
                output="Dream Core revision already committed for this batch.",
                summary="Dream Core revision replayed safely.",
            )
        return SkillResult(
            output="Dream Core revision committed with a snapshot.",
            summary="Dream Core revision committed.",
            state_changed=True,
        )

    async def _curate(self, args: dict) -> SkillResult:
        if set(args) != {"operations"}:
            return SkillResult(
                output="Dream curation accepts only the operations array.",
                success=False,
            )
        if self.curation_succeeded:
            return SkillResult(
                output="Dream curation already completed.",
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
                expected_versions={key: value["version"] for key, value in self.memory_snapshots.items()},
            )
        except (MemoryCurationError, TypeError, KeyError) as exc:
            return SkillResult(
                output=f"Dream curation rejected: {exc}",
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
                "version": digest([item["content"], sorted(item["evidence_message_ids"])]),
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
        receipt = json.dumps(receipt_payload, ensure_ascii=False, separators=(",", ":"))
        return SkillResult(
            output=receipt,
            summary=(
                "Dream Memory curation "
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
        try:
            result = await asyncio.to_thread(
                curate_relationships,
                self.user_id,
                set(self.memory_snapshots),
                self._relationship_operations(args["operations"]),
                batch_id=self.context.period_key,
            )
        except (RelationshipCurationError, MemoryCurationError, TypeError, KeyError) as exc:
            return SkillResult(
                output=f"Dream relationship curation rejected: {exc}",
                success=False,
            )
        receipt = (
            "Dream relationship curation committed: "
            f"upserted={list(result.upserted_ids)}, "
            f"archived={list(result.archived_ids)}."
        )
        changed_ids = (*result.upserted_ids, *result.archived_ids)
        return SkillResult(
            output="Dream relationship curation replayed safely." if result.replayed else receipt,
            summary="Dream relationship curation replayed safely." if result.replayed else receipt,
            entity_refs=[
                f"relationship:{relationship_id}"
                for relationship_id in changed_ids
            ],
            state_changed=bool(changed_ids) and not result.replayed,
        )


def create_dream_session(
    *,
    user_id: int,
    logical_date: str,
    period_key: str,
    channel_id: int = 0,
    transport: str = "",
    core_content: str | None = None,
) -> DreamSession:
    core_content = read_core() if core_content is None else core_content
    conn = _connect()
    try:
        curation_done = conn.execute(
            "SELECT 1 FROM weekly_curation_batches WHERE user_id = ? AND period_key = ?",
            (user_id, period_key),
        ).fetchone() is not None
    finally:
        conn.close()
    return DreamSession(
        user_id=user_id,
        channel_id=channel_id,
        transport=transport,
        expected_core=core_content,
        context=build_dream_context(
            user_id=user_id,
            logical_date=logical_date,
            period_key=period_key,
            core_content=core_content,
        ),
        core_succeeded=has_dream_core_update(user_id, period_key),
        curation_succeeded=curation_done,
    )
