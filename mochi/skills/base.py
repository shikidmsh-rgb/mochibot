"""Skill base class and SKILL.md parser.

Two modes:
1. SKILL.md: tool definitions live in a sibling SKILL.md file (parsed automatically)
2. Classic: subclass overrides get_tools() with Python dicts

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


def _parse_skill_md(md_path: str) -> dict:
    """Parse a SKILL.md file -> {meta, tools, expose_as_tool, triggers}.

    Expected format:
      ---
      name: reminder
      expose: true
      triggers: [tool_call]
      ---

      ## Tool: manage_reminder
      Description: ...

      ### Parameters
      | Name | Type | Required | Description |
      |------|------|----------|-------------|
      | action | string | yes | ... |
    """
    result = {"meta": {}, "tools": [], "expose_as_tool": True, "triggers": ["tool_call"]}

    if not os.path.exists(md_path):
        return result

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Parse front matter
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        for line in fm_match.group(1).strip().split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip()
                if key == "expose":
                    result["expose_as_tool"] = val.lower() in ("true", "yes", "1")
                elif key == "triggers":
                    # Parse [tool_call, cron] format
                    triggers = re.findall(r"\w+", val)
                    result["triggers"] = triggers if triggers else ["tool_call"]
                else:
                    result["meta"][key] = val

    # Parse tool definitions
    tool_blocks = re.split(r"^## Tool:\s*", content, flags=re.MULTILINE)[1:]
    for block in tool_blocks:
        lines = block.strip().split("\n")
        tool_name = lines[0].strip()

        # Extract description
        desc = ""
        desc_match = re.search(r"Description[:\s]*(.+?)(?:\n##|\n###|\Z)",
                               block, re.DOTALL | re.IGNORECASE)
        if desc_match:
            desc = desc_match.group(1).strip().split("\n")[0].strip()

        # Extract parameters from markdown table
        params = {}
        required_params = []
        table_match = re.findall(
            r"\|\s*(\w+)\s*\|\s*(\w+)\s*\|\s*(yes|no|true|false)\s*\|\s*(.+?)\s*\|",
            block, re.IGNORECASE,
        )
        for pname, ptype, req, pdesc in table_match:
            if pname.lower() == "name":  # Skip header row
                continue
            params[pname] = {
                "type": ptype.lower(),
                "description": pdesc.strip(),
            }
            if req.lower() in ("yes", "true"):
                required_params.append(pname)

        # Build OpenAI-compatible tool schema
        tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": required_params,
                },
            },
        }
        result["tools"].append(tool)

    return result


class Skill(ABC):
    """Base class for all MochiBot skills."""

    def __init__(self):
        self._skill_md: dict | None = None
        self._name: str = ""

    @property
    def name(self) -> str:
        if self._name:
            return self._name
        # Infer from class module path
        module = self.__class__.__module__ or ""
        parts = module.split(".")
        # e.g., mochi.skills.reminder.handler → reminder
        if len(parts) >= 3:
            self._name = parts[-2]
        else:
            self._name = self.__class__.__name__.lower()
        return self._name

    @property
    def skill_md(self) -> dict:
        """Parsed SKILL.md content (cached)."""
        if self._skill_md is None:
            # Look for SKILL.md in the same directory as handler.py
            handler_file = os.path.abspath(
                os.path.dirname(self.__class__.__module__.replace(".", "/") + ".py")
            )
            # Fallback: use __file__ from subclass if available
            if hasattr(self, "__module_file__"):
                handler_file = os.path.dirname(self.__module_file__)
            md_path = os.path.join(handler_file, "SKILL.md")
            self._skill_md = _parse_skill_md(md_path)
        return self._skill_md

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
    def triggers(self) -> list[str]:
        """How this skill can be invoked: tool_call, heartbeat, cron, slash."""
        return self.skill_md.get("triggers", ["tool_call"])

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
