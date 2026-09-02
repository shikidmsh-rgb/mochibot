"""Run-scoped tool definitions shared by provider schema and dispatch checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AvailableTool:
    """One immutable tool definition and how it entered the current run."""

    name: str
    definition_json: str
    source: str

    def definition(self) -> dict:
        return json.loads(self.definition_json)


@dataclass(frozen=True)
class ToolAvailability:
    """Immutable snapshot of tools executable in one Main tool loop."""

    entries: tuple[AvailableTool, ...] = ()

    @classmethod
    def from_definitions(
        cls,
        definitions: Iterable[dict],
        *,
        source: str,
    ) -> "ToolAvailability":
        return cls().with_definitions(definitions, source=source)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(entry.name for entry in self.entries)

    def allows(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self.names

    def provider_tools(self) -> list[dict]:
        """Return fresh mutable copies for provider adapters."""
        return [entry.definition() for entry in self.entries]

    def source_for(self, tool_name: str) -> str | None:
        for entry in self.entries:
            if entry.name == tool_name:
                return entry.source
        return None

    def parameters_for(self, tool_name: str) -> dict | None:
        """Return a fresh copy of the current round's argument schema."""
        for entry in self.entries:
            if entry.name != tool_name:
                continue
            function = entry.definition().get("function")
            if not isinstance(function, dict):
                return None
            parameters = function.get("parameters")
            return parameters if isinstance(parameters, dict) else None
        return None

    def validate_arguments(self, tool_name: str, arguments: object) -> str | None:
        """Validate arguments against the exact schema exposed this round."""
        schema = self.parameters_for(tool_name)
        if schema is None:
            return None if isinstance(arguments, dict) else "arguments must be an object"
        return _validate_schema(arguments, schema, path="arguments")

    def with_definitions(
        self,
        definitions: Iterable[dict],
        *,
        source: str,
    ) -> "ToolAvailability":
        """Return a new snapshot with valid, genuinely new definitions appended."""
        existing = set(self.names)
        additions: list[AvailableTool] = []
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            function = definition.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name or name in existing:
                continue
            provider_definition = {
                key: value
                for key, value in definition.items()
                if not key.startswith("_")
            }
            try:
                definition_json = json.dumps(
                    provider_definition,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                continue
            additions.append(AvailableTool(
                name=name,
                definition_json=definition_json,
                source=source,
            ))
            existing.add(name)
        if not additions:
            return self
        return ToolAvailability(self.entries + tuple(additions))


def unavailable_tool_error(tool_name: object) -> str:
    """Stable provider-facing error without revealing hidden registry contents."""
    return json.dumps({
        "ok": False,
        "error": "tool_not_available_this_turn",
        "tool": tool_name if isinstance(tool_name, str) else "",
        "hint": "Use request_tools first",
    }, ensure_ascii=False)


def tool_call_error(tool_name: object, code: str, message: str) -> str:
    """Build a paired model-facing failure for a call that was not executed."""
    return json.dumps({
        "ok": False,
        "error": code,
        "tool": tool_name if isinstance(tool_name, str) else "",
        "message": message,
        "retryable": True,
    }, ensure_ascii=False)


def _validate_schema(value: object, schema: dict, *, path: str) -> str | None:
    """Validate the small JSON Schema subset used by MochiBot tools."""
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of {enum!r}"

    expected_type = schema.get("type")
    if isinstance(expected_type, str):
        if not _matches_type(value, expected_type):
            return f"{path} must be {expected_type}"
    elif isinstance(expected_type, list):
        allowed_types = [
            item for item in expected_type if isinstance(item, str)
        ]
        if allowed_types and not any(
            _matches_type(value, item) for item in allowed_types
        ):
            return f"{path} must be one of {allowed_types!r}"

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    return f"{path}.{key} is required"

        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, dict) else {}
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            item_schema = property_schemas.get(key)
            if isinstance(item_schema, dict):
                error = _validate_schema(item, item_schema, path=f"{path}.{key}")
                if error:
                    return error
            elif additional is False:
                return f"{path}.{key} is not allowed"
            elif isinstance(additional, dict):
                error = _validate_schema(item, additional, path=f"{path}.{key}")
                if error:
                    return error

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return f"{path} must contain at least {min_items} items"
        if isinstance(max_items, int) and len(value) > max_items:
            return f"{path} must contain at most {max_items} items"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                error = _validate_schema(
                    item, item_schema, path=f"{path}[{index}]",
                )
                if error:
                    return error

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(
            isinstance(option, dict)
            and _validate_schema(value, option, path=path) is None
            for option in alternatives
        ):
            return f"{path} does not match any allowed shape"

    return None


def _matches_type(value: object, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
