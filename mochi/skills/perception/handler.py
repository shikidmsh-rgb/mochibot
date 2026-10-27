"""Read safe projections from the existing observer cache."""

from __future__ import annotations

import json
from copy import deepcopy

import mochi.observers as observers
from mochi.skills.base import Skill, SkillContext, SkillResult


def _compact_json(payload: dict) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _error(code: str, message: str) -> SkillResult:
    return SkillResult(
        output=_compact_json({
            "ok": False,
            "error": code,
            "message": message,
        }),
        success=False,
    )


class PerceptionSkill(Skill):
    def get_tools(self) -> list[dict]:
        definitions = deepcopy(super().get_tools())
        if not definitions:
            return definitions
        parameters = definitions[0]["function"]["parameters"]
        parameters["additionalProperties"] = False
        sources = parameters["properties"]["sources"]
        sources["maxItems"] = 3
        sources["uniqueItems"] = True
        sources["items"].update({
            "minLength": 1,
            "pattern": r".*\S.*",
        })
        return definitions

    async def execute(self, context: SkillContext) -> SkillResult:
        if context.actor != "main":
            return _error(
                "main_only",
                "look_around is available only to the Main agent.",
            )
        if context.tool_name != "look_around":
            return _error("unknown_tool", f"Unknown tool: {context.tool_name}")
        if not isinstance(context.args, dict):
            return _error("invalid_arguments", "arguments must be an object")

        extra = set(context.args) - {"sources"}
        if extra:
            return _error(
                "invalid_arguments",
                f"unsupported properties: {', '.join(sorted(extra))}",
            )

        raw_sources = context.args.get("sources")
        if "sources" not in context.args or raw_sources == []:
            mode = "overview"
            requested = None
        else:
            if not isinstance(raw_sources, list):
                return _error("invalid_arguments", "sources must be an array")
            if len(raw_sources) > 3:
                return _error(
                    "invalid_arguments",
                    "sources may contain at most 3 items",
                )

            requested = []
            seen: set[str] = set()
            for source in raw_sources:
                if not isinstance(source, str):
                    return _error(
                        "invalid_arguments",
                        "each source must be a string",
                    )
                normalized = source.strip()
                if not normalized:
                    return _error(
                        "invalid_arguments",
                        "sources must not contain blank names",
                    )
                if normalized in seen:
                    return _error(
                        "invalid_arguments",
                        "sources must be unique after trimming whitespace",
                    )
                seen.add(normalized)
                requested.append(normalized)
            mode = "detail"

        views = observers.read_cached_views(
            requested,
            detail=mode == "detail",
        )
        return SkillResult(
            output=_compact_json({
                "mode": mode,
                "sources": views,
            }),
            history_foldable=bool(views) and all(
                view["available"] and not view["stale"] and not view["error"]
                and not view["facts"].get("view_truncated")
                for view in views
            ),
        )
