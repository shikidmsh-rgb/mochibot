"""Main-only personal authoring with distinct text, execution, and activation effects."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

from mochi import mochi_files_store as documents
from mochi import personal_workspace as workspace
from mochi.extensions import runner, store, template
from mochi.skills.base import Skill, SkillContext, SkillResult

_GUIDE = Path(__file__).resolve().parents[3] / "docs" / "extensions.md"
_TOOLS = {"browse_workspace", "edit_workspace", "run_extension", "activate_extension"}


def _text(args: dict, key: str, *, empty: bool = False) -> str:
    value = args.get(key)
    if not isinstance(value, str) or (not empty and not value):
        raise documents.InvalidArgumentsError(f"{key} must be {'a string' if empty else 'a nonempty string'}")
    return value


def _only(args: dict, allowed: set[str]) -> None:
    if set(args) - allowed:
        raise documents.InvalidArgumentsError("Unsupported properties for this operation")


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _failure(exc: Exception, *, unknown: bool = False) -> SkillResult:
    code = getattr(exc, "code", "invalid_workspace_operation")
    return SkillResult(
        output=_json({"ok": False, "error": code, "message": str(exc)}),
        success=False, error_code=code, retryable=getattr(exc, "retryable", False),
        summary=f"Personal workspace operation failed: code={code}.",
        state_changed=bool(getattr(exc, "state_changed", False)),
        state_change_unknown=unknown or bool(getattr(exc, "state_change_unknown", False)),
    )


class PersonalWorkspaceSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        for definition in definitions:
            parameters = definition["function"]["parameters"]
            parameters["additionalProperties"] = False
            props = parameters["properties"]
            props["path"].update({"minLength": 1, "maxLength": 500})
            if "offset" in props:
                props["offset"]["minimum"] = 0
                props["limit"].update({"minimum": 1, "maximum": workspace.MAX_READ_CHARS})
                props["query"].update({"minLength": 1, "maxLength": documents.MAX_SEARCH_QUERY_CHARS})
                props["paths"].update({
                    "minItems": 1, "maxItems": workspace.MAX_READ_FILES,
                    "items": {"type": "string", "minLength": 1, "maxLength": 500},
                })
            if "files" in props:
                props["files"].update({
                    "maxItems": store.MAX_PACKAGE_FILES,
                    "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string", "minLength": 1, "maxLength": 240},
                            "content": {"type": "string", "maxLength": store.MAX_FILE_BYTES},
                        },
                    },
                })
                props["old_text"]["minLength"] = 1
            if "arguments" in props:
                props["arguments"]["items"] = {"type": "string"}
                props["script"].update({"minLength": 1, "maxLength": 240})
        return definitions

    def available_tools(self) -> list[dict]:
        tools = self.get_tools()
        development = workspace.development_enabled()
        hidden = set()
        if not development:
            hidden.update({"run_extension", "activate_extension"})
        if not any(workspace.file_scope_available(kind) for kind in ("documents", "extensions")):
            hidden.add("browse_workspace")
        if not (
            workspace.file_scope_available("documents", write=True)
            or (development and workspace.file_scope_available("extensions", write=True))
        ):
            hidden.add("edit_workspace")
        return [
            tool for tool in tools
            if tool["function"]["name"] not in hidden
        ]

    def _browse(self, args: dict) -> dict:
        action = _text(args, "action")
        allowed = {
            "list": {"action", "path", "offset", "limit"},
            "search": {"action", "path", "query", "offset", "limit"},
            "read": {"action", "path", "paths", "offset", "limit"},
            "guide": {"action", "path"},
            "report": {"action", "path"},
        }
        if action not in allowed:
            raise documents.InvalidArgumentsError("action must be list, search, read, guide, or report")
        _only(args, allowed[action])
        if "path" in args:
            _text(args, "path")
        offset = args.get("offset", 0)
        if action == "list":
            return workspace.list_workspace(args.get("path"), offset=offset, limit=args.get("limit", 100))
        if action == "search":
            return workspace.search_workspace(
                _text(args, "path"), _text(args, "query"), offset=offset, limit=args.get("limit", 20),
            )
        if action == "read":
            if ("path" in args) == ("paths" in args):
                raise documents.InvalidArgumentsError("read requires exactly one of path or paths")
            return workspace.read_workspace(
                [args["path"]] if "path" in args else args["paths"],
                offset=offset, limit=args.get("limit", workspace.MAX_READ_CHARS),
            )
        target = workspace.draft_address(
            args.get("path", "extensions/local_example/draft") if action == "guide" else _text(args, "path"),
        )
        workspace.require_file_scope("extensions")
        if action == "guide":
            return {
                "action": "guide", "guide": _GUIDE.read_text(encoding="utf-8"),
                "draft_path": target.draft_path, "template": template.files(target.name),
                "run_script": "smoke.py", "development_enabled": workspace.development_enabled(),
            }
        return {"action": "report", "draft_path": target.draft_path, **runner.read_last_run(target.name)}

    def _edit(self, args: dict) -> dict:
        action = _text(args, "action")
        allowed = {
            "create": {"action", "path", "content", "files"},
            "append": {"action", "path", "content"},
            "edit": {"action", "path", "old_text", "new_text"},
            "replace": {"action", "path", "content"},
            "remove": {"action", "path"},
        }
        if action not in allowed:
            raise documents.InvalidArgumentsError("action must be create, append, edit, replace, or remove")
        _only(args, allowed[action])
        path = _text(args, "path")
        target = workspace.address(path)
        if action == "edit":
            _text(args, "old_text")
            _text(args, "new_text", empty=True)
        elif action in {"append", "replace"} or (
            action == "create" and (target.kind == "documents" or target.relative)
        ):
            _text(args, "content", empty=True)
        return workspace.edit_workspace(action, path, args)

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.actor != "main" or context.trigger != "tool_call":
            return _failure(store.ExtensionError("main_only", "Personal workspace is available only to Main tool calls."))
        if context.tool_name not in _TOOLS:
            return _failure(store.ExtensionError("unknown_tool", "Unknown personal workspace tool."))
        if not isinstance(context.args, dict):
            return _failure(documents.InvalidArgumentsError("arguments must be an object"))
        args, tool = context.args, context.tool_name
        try:
            if tool == "browse_workspace":
                payload = await asyncio.to_thread(self._browse, args)
            elif tool == "edit_workspace":
                payload = await asyncio.to_thread(self._edit, args)
            else:
                _only(args, {"path", "script", "arguments"} if tool == "run_extension" else {"path"})
                target = workspace.draft_address(_text(args, "path"))
                workspace.require_development()
                if tool == "run_extension":
                    if "script" in args:
                        _text(args, "script")
                    if "arguments" in args and (
                        not isinstance(args["arguments"], list)
                        or any(not isinstance(item, str) or "\x00" in item for item in args["arguments"])
                    ):
                        raise documents.InvalidArgumentsError("arguments must be an array of strings without NUL")
                    payload = await runner.run_extension(
                        target.name, script=args.get("script", "smoke.py"), arguments=args.get("arguments"),
                    )
                    payload["draft_path"] = target.draft_path
                    success = bool(payload["success"] and payload.get("report_saved"))
                    return SkillResult(
                        output=_json(payload), success=success,
                        error_code="" if success else (
                            "extension_timeout" if payload.get("timed_out")
                            else "run_report_failed" if not payload.get("report_saved")
                            else "extension_run_failed"
                        ),
                        retryable=False if not success else None,
                        summary=(
                            f"Personal extension script {'completed' if payload['success'] else 'failed'}; "
                            f"run report {'saved' if payload.get('report_saved') else 'not saved'}; "
                            "this does not activate the extension."
                        ),
                        entity_refs=[f"extension:{target.name}"],
                        state_changed=bool(payload.get("report_saved")),
                        state_change_unknown=bool(payload.get("state_change_unknown")),
                    )
                from mochi.skills import activate_extension

                payload = {**activate_extension(target.name), "draft_path": target.draft_path}
        except (OSError, ValueError) as exc:
            return _failure(
                exc,
                unknown=tool == "edit_workspace" and isinstance(exc, documents.StorageIOError),
            )
        action = payload.get("action", "activate")
        return SkillResult(
            output=_json(payload),
            summary=f"Personal workspace {tool}: {action} completed.",
            entity_refs=[f"extension:{payload['name']}"] if payload.get("name") else [],
            state_changed=tool != "browse_workspace" and bool(payload.get("state_changed", True)),
            state_change_unknown=bool(payload.get("state_change_unknown")),
            content_source="agent_authored_document" if tool == "browse_workspace" else "",
        )
