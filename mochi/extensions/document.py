"""Read-only personal guidance bound to an activated extension snapshot."""

import hashlib
import json
from pathlib import Path

from mochi.skills.base import Skill, SkillContext, SkillResult

from . import store


def tool_definition(name: str, description: str) -> dict:
    return {
        "type": "function",
        "_load": "routed",
        "_adaptive_load": True,
        "function": {
            "name": f"{name}_read",
            "description": (
                f"Read your saved guide for: {description}. Returns the activated text, "
                "not a completed task. Long guides are paged; next_offset continues the read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "offset": {
                        "type": "integer", "minimum": 0,
                        "description": "Character offset in the body; default 0.",
                    },
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": store.MAX_READ_CHARS,
                        "description": "Characters per page; default and maximum 12000.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


class DocumentSkill(Skill):
    def __init__(self, package_dir: Path, parsed: dict):
        super().__init__()
        self.external = True
        self.__module_file__ = str(package_dir / "SKILL.md")
        self._skill_md = parsed
        self._populate_from_md(parsed)
        self._body = parsed["document_body"]
        self._version = hashlib.sha256(
            store._read_bytes(package_dir / "SKILL.md"),
        ).hexdigest()

    async def execute(self, context: SkillContext) -> SkillResult:
        from mochi.tool_availability import _validate_schema

        error = _validate_schema(
            context.args, self.get_tools()[0]["function"]["parameters"], path="arguments",
        )
        if error:
            return SkillResult(
                output=error, success=False, error_code="invalid_tool_arguments", retryable=True,
            )
        offset = context.args.get("offset", 0)
        limit = context.args.get("limit", store.MAX_READ_CHARS)
        try:
            store._page(offset, limit, store.MAX_READ_CHARS)
        except store.ExtensionError as exc:
            return SkillResult(
                output=str(exc), success=False, error_code=exc.code, retryable=True,
            )
        start = min(offset, len(self._body))
        end = min(start + limit, len(self._body))
        complete = end == len(self._body)
        return SkillResult(
            output=json.dumps({
                "name": self.name, "version": self._version,
                "content": self._body[start:end], "characters": len(self._body),
                "offset": start, "next_offset": None if complete else end,
                "complete": complete, "truncated": not complete,
            }, ensure_ascii=False),
            summary=f"Read document skill {self.name}: characters {start}-{end} of {len(self._body)}.",
            entity_refs=[f"extension:{self.name}"],
            content_source="agent_authored_document",
        )
