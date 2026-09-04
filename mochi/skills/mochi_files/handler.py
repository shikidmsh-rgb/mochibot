"""Thin Main-only adapter for private Mochi Files storage."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

from mochi import mochi_files_store as store
from mochi.skills.base import Skill, SkillContext, SkillResult


def _compact_json(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _summary(payload: dict) -> str:
    action = payload.get("action", "unknown")
    path = payload.get("path") or payload.get("scope") or ""
    facts = [f"action={action}"]
    if path:
        facts.append(f"path={path}")
    for key in ("bytes", "count", "total", "total_matches", "offset", "end_offset"):
        if key in payload:
            facts.append(f"{key}={payload[key]}")
    facts.append(f"complete={payload.get('complete', True)}")
    return "Mochi Files: " + ", ".join(facts)


def _error(code: str, message: str, *, retryable: bool = False) -> SkillResult:
    return SkillResult(
        output=_compact_json({
            "ok": False,
            "error": code,
            "message": message,
        }),
        success=False,
        summary=f"Mochi Files failed: code={code}",
        error_code=code,
        retryable=retryable,
    )


class MochiFilesSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            function = definition["function"]
            parameters = function["parameters"]
            parameters["additionalProperties"] = False
            properties = parameters["properties"]
            properties["path"].update({"minLength": 1, "maxLength": 500})
            if "query" in properties:
                properties["query"].update({
                    "minLength": 1,
                    "maxLength": store.MAX_SEARCH_QUERY_CHARS,
                })
            if "offset" in properties:
                properties["offset"]["minimum"] = 0
            if "limit" in properties:
                properties["limit"].update({
                    "minimum": 1,
                    "maximum": store.MAX_READ_CHARS,
                })
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.trigger != "tool_call" or context.actor != "main":
            return _error(
                "main_only",
                "Mochi Files is available only to sovereign Main tool calls.",
            )
        if context.tool_name not in {"browse_mochi_files", "save_mochi_file"}:
            return _error("unknown_tool", f"Unknown tool: {context.tool_name}")
        if not isinstance(context.args, dict):
            return _error("invalid_arguments", "arguments must be an object")
        try:
            if context.tool_name == "browse_mochi_files":
                payload = await asyncio.to_thread(self._browse, context.args)
                return SkillResult(
                    output=_compact_json(payload),
                    summary=_summary(payload),
                    content_source="agent_authored_document",
                )
            payload = await asyncio.to_thread(self._save, context.args)
            return SkillResult(
                output=_compact_json(payload),
                summary=_summary(payload),
                state_changed=True,
            )
        except store.MochiFilesError as exc:
            return _error(exc.code, str(exc), retryable=exc.retryable)

    def _browse(self, args: dict) -> dict:
        action = args.get("action")
        allowed_by_action = {
            "list": {"action", "path", "offset", "limit"},
            "search": {"action", "path", "query", "offset", "limit"},
            "read": {"action", "path", "offset", "limit"},
        }
        if action not in allowed_by_action:
            raise store.InvalidArgumentsError("action must be list, search, or read")
        extra = set(args) - allowed_by_action[action]
        if extra:
            raise store.InvalidArgumentsError(
                f"unsupported properties for {action}: {', '.join(sorted(extra))}"
            )
        offset = args.get("offset", 0)
        if action == "list":
            return store.list_files(
                path=args.get("path"),
                offset=offset,
                limit=args.get("limit", store.MAX_LIST_RESULTS),
            )
        if action == "search":
            if "query" not in args:
                raise store.InvalidArgumentsError("query is required for search")
            return store.search_files(
                args["query"],
                path=args.get("path"),
                offset=offset,
                limit=args.get("limit", store.MAX_SEARCH_RESULTS),
            )
        if "path" not in args:
            raise store.InvalidArgumentsError("path is required for read")
        return store.read_file(
            args["path"],
            offset=offset,
            limit=args.get("limit", store.MAX_READ_CHARS),
        )

    def _save(self, args: dict) -> dict:
        action = args.get("action")
        allowed_by_action = {
            "create": {"action", "path", "content"},
            "append": {"action", "path", "content"},
            "edit": {"action", "path", "old_text", "new_text"},
        }
        if action not in allowed_by_action:
            raise store.InvalidArgumentsError("action must be create, append, or edit")
        extra = set(args) - allowed_by_action[action]
        if extra:
            raise store.InvalidArgumentsError(
                f"unsupported properties for {action}: {', '.join(sorted(extra))}"
            )
        if "path" not in args:
            raise store.InvalidArgumentsError("path is required")
        if action in {"create", "append"}:
            if "content" not in args:
                raise store.InvalidArgumentsError(
                    f"content is required for {action}"
                )
            operation = store.create_file if action == "create" else store.append_file
            return operation(args["path"], args["content"])
        if "old_text" not in args or "new_text" not in args:
            raise store.InvalidArgumentsError(
                "old_text and new_text are required for edit"
            )
        return store.edit_file(args["path"], args["old_text"], args["new_text"])
