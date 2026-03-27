"""Skill base class and SKILL.md parser (v2).

Supports two SKILL.md formats:
  v1 (legacy): ``## Tool: tool_name`` sections, ``expose: true`` front-matter
  v2 (current): ``## Tools`` / ``### tool_name`` sections, ``expose_as_tool: true``

The parser auto-detects which format is in use.

Every skill directory must have:
  - SKILL.md       (tool definitions + metadata)
  - handler.py     (execution logic)
  - __init__.py
"""

import os
import re
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class SkillContext:
    """Unified invocation context passed to Skill.run().

    All callers (tool_call, heartbeat, cron, slash, script) build this
    and pass it in. The skill doesn't need to know who called it.
    """
    trigger: str            # "tool_call" | "heartbeat" | "cron" | "slash" | "script"
    user_id: int = 0
    channel_id: int = 0
    tool_name: str = ""     # only set for trigger="tool_call"
    args: dict = field(default_factory=dict)
    observation: dict | None = None  # only set for trigger="heartbeat"


@dataclass
class SkillResult:
    """Unified result returned by Skill.run().

    - output: text string (fed back to LLM or logged)
    - actions: heartbeat-style action list [{"type": "message", "content": ...}]
    - success: whether the skill executed without error
    """
    output: str = ""
    actions: list[dict] = field(default_factory=list)
    success: bool = True


# ---------------------------------------------------------------------------
# SKILL.md Parsing (v1 + v2 dual-format)
# ---------------------------------------------------------------------------

def _parse_skill_md(md_path: str) -> dict:
    """Parse a SKILL.md file -> {meta, tools, expose_as_tool, triggers, usage_rules, type, multi_turn}.

    Supports two formats:
      v1: ``## Tool: name`` + ``expose: true`` + ``triggers: [tool_call]``
      v2: ``## Tools`` / ``### name`` + ``expose_as_tool: true`` + ``type: tool``

    Auto-detects based on content headers.
    """
    result: dict = {
        "meta": {},
        "tools": [],
        "expose_as_tool": True,
        "triggers": ["tool_call"],
        "usage_rules": "",
        "type": "tool",
        "multi_turn": False,
        "requires_config": [],
    }

    if not os.path.exists(md_path):
        return result

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # ── Parse front matter ──
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).strip().split("\n"):
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if key == "expose" or key == "expose_as_tool":
                result["expose_as_tool"] = val.lower() in ("true", "yes", "1")
            elif key == "triggers":
                triggers = re.findall(r"\w+", val)
                result["triggers"] = triggers if triggers else ["tool_call"]
            elif key == "type":
                result["type"] = val
            elif key == "multi_turn":
                result["multi_turn"] = val.lower() in ("true", "yes", "1")
            elif key == "requires_config":
                keys = re.findall(r"[A-Z_][A-Z0-9_]+", val)
                result["requires_config"] = keys
            else:
                result["meta"][key] = val

    # ── Extract usage rules ──
    result["usage_rules"] = _extract_usage_rules(content)

    # ── Detect format and parse tools ──
    if re.search(r"^## Tools\s*$", content, re.MULTILINE):
        # v2 format: ## Tools / ### tool_name
        result["tools"] = _parse_tools_v2(content)
    elif re.search(r"^## Tool:\s*", content, re.MULTILINE):
        # v1 format: ## Tool: tool_name
        result["tools"] = _parse_tools_v1(content)

    return result


def _extract_usage_rules(content: str) -> str:
    """Extract ## Usage Rules / ## Behavior Rules sections from SKILL.md."""
    rules_parts: list[str] = []
    for header in ("Usage Rules", "Behavior Rules", "Response Gotchas", "Category Guide"):
        pattern = rf"^## {re.escape(header)}\s*\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
        if match:
            rules_parts.append(match.group(1).strip())
    return "\n\n".join(rules_parts) if rules_parts else ""


def _parse_tools_v1(content: str) -> list[dict]:
    """Parse v1 format: ``## Tool: tool_name`` sections."""
    tools = []
    tool_blocks = re.split(r"^## Tool:\s*", content, flags=re.MULTILINE)[1:]
    for block in tool_blocks:
        lines = block.strip().split("\n")
        tool_name = lines[0].strip()

        desc = ""
        desc_match = re.search(r"Description[:\s]*(.+?)(?:\n##|\n###|\Z)",
                               block, re.DOTALL | re.IGNORECASE)
        if desc_match:
            desc = desc_match.group(1).strip().split("\n")[0].strip()

        params, required_params = _parse_param_table(block)
        tools.append(_build_tool_schema(tool_name, desc, params, required_params))
    return tools


def _parse_tools_v2(content: str) -> list[dict]:
    """Parse v2 format: ``## Tools`` then ``### tool_name`` sub-sections."""
    tools = []
    # Find the ## Tools section
    tools_match = re.search(r"^## Tools\s*\n(.*?)(?=\n## |\Z)", content, re.MULTILINE | re.DOTALL)
    if not tools_match:
        return tools

    tools_section = tools_match.group(1)
    tool_blocks = re.split(r"^### ", tools_section, flags=re.MULTILINE)[1:]
    for block in tool_blocks:
        lines = block.strip().split("\n")
        tool_name = lines[0].strip()

        # Description is the first non-empty line after the tool name
        desc = ""
        for line in lines[1:]:
            line = line.strip()
            if line and not line.startswith("|") and not line.startswith("-"):
                desc = line
                break

        params, required_params = _parse_param_table(block)
        tools.append(_build_tool_schema(tool_name, desc, params, required_params))
    return tools


def _parse_param_table(block: str) -> tuple[dict, list[str]]:
    """Extract parameters from a markdown table in a tool block."""
    params: dict = {}
    required: list[str] = []
    table_match = re.findall(
        r"\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(yes|no|true|false)\s*\|\s*(.+?)\s*\|",
        block, re.IGNORECASE,
    )
    for pname, ptype, req, pdesc in table_match:
        if pname.lower() in ("name", "parameter"):
            continue
        params[pname] = {
            "type": ptype.lower(),
            "description": pdesc.strip(),
        }
        if req.lower() in ("yes", "true"):
            required.append(pname)
    return params, required


def _build_tool_schema(name: str, desc: str, params: dict, required: list[str]) -> dict:
    """Build an OpenAI-compatible tool schema dict."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": params,
                "required": required,
            },
        },
    }


# ---------------------------------------------------------------------------
# Skill Base Class (v2)
# ---------------------------------------------------------------------------

class Skill(ABC):
    """Base class for all MochiBot skills (v2).

    v2 attributes (populated from SKILL.md during discovery):
        skill_type  — "tool" | "automation" | "hybrid"
        multi_turn  — sticky skill for follow-ups
        usage_rules — LLM guidance from SKILL.md
        description — human-readable description
    """

    def __init__(self):
        self._skill_md: dict | None = None
        self._name: str = ""
        self.description: str = ""
        self.skill_type: str = "tool"
        self.multi_turn: bool = False
        self.usage_rules: str = ""
        self.requires_config: list[str] = []

    @property
    def name(self) -> str:
        if self._name:
            return self._name
        # Infer from class module path
        module = self.__class__.__module__ or ""
        parts = module.split(".")
        if len(parts) >= 3:
            self._name = parts[-2]
        else:
            self._name = self.__class__.__name__.lower()
        return self._name

    @property
    def skill_md(self) -> dict:
        """Parsed SKILL.md content (cached)."""
        if self._skill_md is None:
            handler_file = os.path.abspath(
                os.path.dirname(self.__class__.__module__.replace(".", "/") + ".py")
            )
            if hasattr(self, "__module_file__"):
                handler_file = os.path.dirname(self.__module_file__)
            md_path = os.path.join(handler_file, "SKILL.md")
            self._skill_md = _parse_skill_md(md_path)
            self._populate_from_md(self._skill_md)
        return self._skill_md

    def _populate_from_md(self, parsed: dict) -> None:
        """Populate v2 attributes from parsed SKILL.md data."""
        meta = parsed.get("meta", {})
        if not self._name and meta.get("name"):
            self._name = meta["name"]
        if not self.description and meta.get("description"):
            self.description = meta["description"]
        self.skill_type = parsed.get("type", "tool")
        self.multi_turn = parsed.get("multi_turn", False)
        self.usage_rules = parsed.get("usage_rules", "")
        self.requires_config = parsed.get("requires_config", [])

    def get_tools(self) -> list[dict]:
        """Return OpenAI-compatible tool definitions.

        Default: parsed from SKILL.md. Override for dynamic tools.
        """
        return self.skill_md.get("tools", [])

    @property
    def expose_as_tool(self) -> bool:
        """Whether this skill's tools should appear in the LLM tools array."""
        return self.skill_md.get("expose_as_tool", True)

    @property
    def triggers(self) -> list:
        """How this skill can be invoked: tool_call, heartbeat, cron, slash."""
        return self.skill_md.get("triggers", ["tool_call"])

    def tool_names(self) -> set[str]:
        """Return set of tool names this skill exposes."""
        return {t["function"]["name"] for t in self.get_tools()}

    def handles(self, tool_name: str) -> bool:
        """Check if this skill handles a specific tool name."""
        return tool_name in self.tool_names()

    def has_trigger(self, trigger_type: str, **kwargs) -> bool:
        """Check if this skill matches a trigger type and optional conditions.

        For simple triggers (list of strings): checks if trigger_type is in list.
        For v2 triggers (list of dicts): checks type field + all kwargs match.
        """
        for t in self.triggers:
            if isinstance(t, str):
                if t == trigger_type:
                    return True
            elif isinstance(t, dict):
                if t.get("type") != trigger_type:
                    continue
                if all(t.get(k) == v for k, v in kwargs.items()):
                    return True
        return False

    @abstractmethod
    async def execute(self, context: SkillContext) -> SkillResult:
        """Execute the skill. Must be implemented by subclasses."""
        ...

    async def run(self, context: SkillContext) -> SkillResult:
        """Unified entry point. Wraps execute() with logging."""
        log.info("Skill %s triggered by %s", self.name, context.trigger)
        try:
            result = await self.execute(context)
            return result
        except Exception as e:
            log.error("Skill %s failed: %s", self.name, e, exc_info=True)
            return SkillResult(output=f"Skill error: {e}", success=False)
