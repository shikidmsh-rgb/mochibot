"""Turn-local native payloads; tool authorization remains in ToolAvailability."""

from copy import deepcopy
from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class NativeOptions:
    explicit_cache: bool = False
    responses_additional_tools: bool = False
    anthropic_deferred_tools: bool = False

    @classmethod
    def from_dict(cls, value: dict | None) -> "NativeOptions":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("Native options must be an object")
        known = cls.__dataclass_fields__
        if any(key not in known or not isinstance(item, bool) for key, item in value.items()):
            raise ValueError("Native options require known boolean fields")
        return cls(**value)

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)


class ResponsesTurn:
    def __init__(
        self, messages: list[dict], tools: list[dict], *, identity: str,
        key: str, options: NativeOptions, resident_names: list[str],
    ):
        from mochi.llm import ResponsesCacheContext, _responses_input, _responses_tools

        self.options = options
        self.cache = ResponsesCacheContext.from_messages(messages, key=key, identity=identity)
        definitions = {tool["name"]: tool for tool in _responses_tools(tools)}
        self.base = [
            deepcopy(definitions[name]) for name in dict.fromkeys(resident_names)
            if name in definitions
        ]
        self.declared = deepcopy(definitions)
        base_names = {tool["name"] for tool in self.base}
        dynamic = [tool for name, tool in definitions.items() if name not in base_names]
        self.declarations: list[tuple[int, list[dict]]] = []
        if dynamic and options.responses_additional_tools:
            self.declarations.append((len(_responses_input(messages)), deepcopy(dynamic)))
        self.tools = deepcopy(tools)

    def request(self, messages: list[dict], tools: list[dict] | None) -> dict:
        from mochi.llm import _responses_input, _responses_tools, _with_stable_cache_boundary

        items = _responses_input(messages)
        if self.options.explicit_cache:
            items = _with_stable_cache_boundary(items, self.cache)
        kwargs: dict = {"input": items}
        if self.options.responses_additional_tools:
            if tools is not None:
                definitions = {tool["name"]: tool for tool in _responses_tools(tools)}
                for name, definition in definitions.items():
                    if name in self.declared and self.declared[name] != definition:
                        raise ValueError(f"Responses tool changed during an active turn: {name}")
                additions = [
                    tool for name, tool in definitions.items() if name not in self.declared
                ]
                if additions:
                    self.declarations.append((len(items), deepcopy(additions)))
                    self.declared.update((tool["name"], deepcopy(tool)) for tool in additions)
            for offset, (position, definitions) in enumerate(self.declarations):
                items.insert(position + offset, {
                    "type": "additional_tools", "role": "developer",
                    "tools": deepcopy(definitions),
                })
            if self.base:
                kwargs["tools"] = deepcopy(self.base)
        else:
            if tools is not None:
                self.tools = deepcopy(tools)
            if self.tools:
                kwargs["tools"] = _responses_tools(self.tools)
        if tools is None:
            kwargs["tool_choice"] = "none"
        if self.options.explicit_cache:
            kwargs["prompt_cache_key"] = self.cache.key
            kwargs["extra_body"] = {
                "prompt_cache_options": {"mode": "implicit", "ttl": "30m"},
            }
        # additional_tools is not part of the SDK's typed input union.
        if self.options.responses_additional_tools and self.declarations:
            kwargs.setdefault("extra_body", {})["input"] = kwargs.pop("input")
        return kwargs


class AnthropicTurn:
    def __init__(
        self, messages: list[dict], tools: list[dict], *, identity: str,
        options: NativeOptions, deferred_catalog: list[dict],
    ):
        from mochi.llm import AnthropicProvider

        self.options = options
        self.identity = identity
        self.history = AnthropicProvider._convert_messages([
            message for message in messages if message["role"] != "system"
        ])
        self.tools = AnthropicProvider._convert_tools(tools)
        self.loads: dict[str, tuple[list[str], dict]] = {}
        if options.anthropic_deferred_tools:
            eager = {tool["name"] for tool in self.tools}
            catalog = AnthropicProvider._convert_tools(deferred_catalog)
            self.tools.extend(
                {**deepcopy(tool), "defer_loading": True}
                for tool in sorted(catalog, key=lambda tool: tool["name"])
                if tool["name"] not in eager
            )

    def record_load(self, call_id: str, definitions: list[dict], result: dict) -> None:
        if self.options.anthropic_deferred_tools and definitions:
            self.loads[call_id] = (
                [tool["function"]["name"] for tool in definitions], deepcopy(result),
            )

    def request(self, messages: list[dict], tools: list[dict] | None) -> dict:
        from mochi.llm import AnthropicProvider

        if tools is not None:
            available = AnthropicProvider._convert_tools(tools)
            if self.options.anthropic_deferred_tools:
                declared = {tool["name"]: tool for tool in self.tools}
                for tool in available:
                    old = declared.get(tool["name"])
                    if old is None:
                        self.tools.append({**deepcopy(tool), "defer_loading": True})
                    elif {k: v for k, v in old.items() if k != "defer_loading"} != tool:
                        raise ValueError(f"Anthropic tool changed during an active turn: {tool['name']}")
            else:
                self.tools = available
        system = [
            {"type": "text", "text": message["content"]}
            for message in messages if message["role"] == "system" and message["content"]
        ]
        conversation = AnthropicProvider._convert_messages([
            message for message in messages if message["role"] != "system"
        ])
        kwargs: dict = {"messages": conversation}
        if self.options.anthropic_deferred_tools:
            declared = {tool["name"]: tool for tool in self.tools}
            for message in conversation:
                if not isinstance(message.get("content"), list):
                    continue
                annotations = []
                for block in message["content"]:
                    if block.get("type") != "tool_result":
                        continue
                    call_id = block["tool_use_id"]
                    if call_id not in self.loads:
                        continue
                    names, result = self.loads[call_id]
                    if any(name not in declared for name in names):
                        raise ValueError("Loaded tool is absent from the Anthropic catalog")
                    references = [
                        {"type": "tool_reference", "tool_name": name}
                        for name in names if declared[name].get("defer_loading")
                    ]
                    if references:
                        block["content"] = references
                        annotations.append({
                            "type": "text",
                            "text": json.dumps({
                                "tool_use_id": call_id, "tool_loading_result": result,
                            }, ensure_ascii=False, separators=(",", ":")),
                        })
                message["content"].extend(annotations)
            kwargs["extra_headers"] = {"anthropic-beta": "advanced-tool-use-2025-11-20"}
        if self.options.explicit_cache:
            if conversation[:len(self.history)] != self.history:
                raise ValueError("Anthropic stable history does not match the request")
            if not system or not system[0]["text"].startswith(self.identity) or not self.identity:
                raise ValueError("Anthropic identity does not match the request")
            remaining = system[0]["text"][len(self.identity):]
            system[:1] = [
                {"type": "text", "text": self.identity, "cache_control": {"type": "ephemeral"}},
                *([{"type": "text", "text": remaining}] if remaining else []),
            ]
            system[-1]["cache_control"] = {"type": "ephemeral"}
            for message in reversed(conversation[:len(self.history)]):
                content = message["content"]
                if isinstance(content, str) and content:
                    content = [{"type": "text", "text": content}]
                    message["content"] = content
                if isinstance(content, list):
                    block = next((
                        block for block in reversed(content)
                        if block.get("type") == "text" and block.get("text")
                    ), None)
                    if block is not None:
                        block["cache_control"] = {"type": "ephemeral"}
                        break
            # At most two system boundaries, one history boundary and this moving boundary.
            kwargs["extra_body"] = {"cache_control": {"type": "ephemeral"}}
        elif system:
            system[-1]["cache_control"] = {"type": "ephemeral"}
        if system:
            kwargs["system"] = system
        if self.tools:
            kwargs["tools"] = deepcopy(self.tools)
        if tools is None:
            kwargs["tool_choice"] = {"type": "none"}
        return kwargs
