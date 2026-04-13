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
    transport: str = ""     # "telegram" | "wechat" — from IncomingMessage
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
# v3 metadata dataclasses (for scan_skill_metadata — no handler imports)
# ---------------------------------------------------------------------------

@dataclass
class ConfigField:
    """One declared config key for a skill (parsed from SKILL.md config: block)."""
    key: str
    type: str           # "int", "float", "bool", "str"
    default: str        # always str — cast by resolver
    description: str = ""
    internal: bool = False  # hidden from admin UI when True


@dataclass
class ToolMeta:
    """Metadata for a single tool parsed from SKILL.md heading."""
    name: str
    skill: str          # logical skill this tool belongs to
    risk_level: str     # L0, L1, L2, L3


@dataclass
class NudgeMeta:
    """Nudge metadata parsed from SKILL.md nudge: block (stored for future use)."""
    requires_awake: bool = True
    check_interval_s: float = 0


@dataclass
class WritesMeta:
    """Data output declaration parsed from SKILL.md writes: block."""
    diary: list[str] = field(default_factory=list)
    db: list[str] = field(default_factory=list)


@dataclass
class SkillMeta:
    """Metadata for a skill parsed from SKILL.md (no handler import needed)."""
    name: str
    description: str
    skill_type: str     # tool, automation, hybrid
    tier: str           # lite, chat, deep
    tools: list[ToolMeta] = field(default_factory=list)
    sub_skills: dict[str, str] = field(default_factory=dict)
    requires_env: list[str] = field(default_factory=list)
    config_schema: list[ConfigField] = field(default_factory=list)
    nudge_meta: NudgeMeta | None = None
    writes_meta: WritesMeta | None = None
    triggers: list[dict] = field(default_factory=list)
    has_sense: bool = False  # whether skill declares sense: block
    exclude_transports: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SKILL.md Parsing (v1 + v2 dual-format)
# ---------------------------------------------------------------------------

def _flush_config_entry(
    key: str, props: dict[str, str],
    schema: list[ConfigField], md_path: str,
) -> None:
    """Validate and append a parsed config entry to the schema list."""
    if key.startswith("_"):
        log.warning("Config key '%s' in %s starts with _ (reserved) — skipped", key, md_path)
        return
    field_type = props.get("type", "").lower()
    if field_type not in ("int", "float", "bool", "str"):
        log.warning("Config key '%s' in %s has invalid/missing type '%s' — skipped", key, md_path, field_type)
        return
    if "default" not in props:
        log.warning("Config key '%s' in %s is missing default — skipped", key, md_path)
        return
    schema.append(ConfigField(
        key=key,
        type=field_type,
        default=props["default"],
        description=props.get("description", ""),
        internal=props.get("internal", "").lower() in ("true", "yes", "1"),
    ))


def _parse_skill_md(md_path: str) -> dict:
    """Parse a SKILL.md file -> {meta, tools, expose_as_tool, triggers, usage_rules, type, tier, ...}.

    Supports two tool formats:
      v1 (legacy): ``## Tool: tool_name`` sections, ``expose: true`` front-matter
      v2 (current): ``## Tools`` / ``### tool_name`` sections, ``expose_as_tool: true``

    Front-matter supports multi-line blocks (v3):
      tier, sub_skills, requires, config, nudge, writes, sense

    Auto-detects tool format based on content headers.
    """
    result: dict = {
        "meta": {},
        "tools": [],
        "expose_as_tool": True,
        "triggers": ["tool_call"],
        "usage_rules": "",
        "type": "tool",
        "tier": "chat",
        "multi_turn": False,
        "requires_config": [],
        "requires_env": [],
        "has_sense": False,
        "core": False,
        "diary": [],
        "diary_status_order": 50,
        "config_schema": [],
        "sub_skills": {},
        "nudge_meta": None,
        "writes_meta": None,
        "exclude_transports": [],
    }

    if not os.path.exists(md_path):
        return result

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # ── Parse front matter with state machine ──
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        # State flags for multi-line blocks
        _in_sub_skills = False
        _in_requires = False
        _in_config = False
        _in_nudge = False
        _in_writes = False
        _in_nested_block = False  # for blocks we skip (sense:, etc.)
        _has_sense = False        # track if sense: block is present

        # Accumulators for multi-line blocks
        _config_key = ""
        _config_props: dict[str, str] = {}
        config_schema: list[ConfigField] = []
        _nudge_props: dict[str, str] = {}
        _writes_diary: list[str] = []
        _writes_db: list[str] = []
        sub_skills: dict[str, str] = {}
        requires_env: list[str] = []

        for line in fm_match.group(1).strip().split("\n"):
            stripped = line.strip()

            # ── Detect block starts ──

            if stripped == "sub_skills:" or stripped.startswith("sub_skills:"):
                inline = stripped.split(":", 1)[1].strip()
                if not inline:
                    _in_sub_skills = True
                    _in_requires = _in_config = _in_nudge = _in_writes = _in_nested_block = False
                    continue

            if stripped == "requires:" or (stripped.startswith("requires:") and not stripped.split(":", 1)[1].strip()):
                _in_requires = True
                _in_sub_skills = _in_config = _in_nudge = _in_writes = _in_nested_block = False
                continue

            if stripped == "sense:" or (stripped.startswith("sense:") and not stripped.split(":", 1)[1].strip()):
                _in_nested_block = True
                _has_sense = True
                _in_sub_skills = _in_requires = _in_config = _in_nudge = _in_writes = False
                continue

            if stripped == "nudge:" or (stripped.startswith("nudge:") and not stripped.split(":", 1)[1].strip()):
                _in_nudge = True
                _in_sub_skills = _in_requires = _in_config = _in_writes = _in_nested_block = False
                _nudge_props = {}
                continue

            if stripped == "writes:" or (stripped.startswith("writes:") and not stripped.split(":", 1)[1].strip()):
                _in_writes = True
                _in_sub_skills = _in_requires = _in_config = _in_nudge = _in_nested_block = False
                _writes_diary = []
                _writes_db = []
                continue

            if stripped == "config:" or (stripped.startswith("config:") and not stripped.split(":", 1)[1].strip()):
                _in_config = True
                _in_sub_skills = _in_requires = _in_nudge = _in_writes = _in_nested_block = False
                _config_key = ""
                _config_props = {}
                continue

            # ── Parse indented block entries ──

            if _in_sub_skills:
                if line.startswith("  ") and ":" in stripped:
                    sk, sv = stripped.split(":", 1)
                    sub_skills[sk.strip()] = sv.strip().strip('"').strip("'")
                    continue
                else:
                    _in_sub_skills = False

            if _in_requires:
                if line.startswith("  ") and ":" in stripped:
                    rk, rv = stripped.split(":", 1)
                    if rk.strip() == "env":
                        val = rv.strip().strip("[]")
                        requires_env = [k.strip() for k in val.split(",") if k.strip()]
                    continue
                else:
                    _in_requires = False

            if _in_nudge:
                if line.startswith("  ") and ":" in stripped:
                    nk, nv = stripped.split(":", 1)
                    _nudge_props[nk.strip()] = nv.strip().strip('"').strip("'")
                    continue
                else:
                    _in_nudge = False

            if _in_writes:
                if line.startswith("  ") and ":" in stripped:
                    wk, wv = stripped.split(":", 1)
                    wk = wk.strip()
                    vals = [v.strip() for v in wv.strip().strip("[]").split(",") if v.strip()]
                    if wk == "diary":
                        _writes_diary = vals
                    elif wk == "db":
                        _writes_db = vals
                    continue
                else:
                    _in_writes = False

            if _in_config:
                if line.startswith("    ") and ":" in stripped:
                    # 4-space indent = property of current config key
                    pk, pv = stripped.split(":", 1)
                    _config_props[pk.strip()] = pv.strip().strip('"').strip("'")
                    continue
                elif line.startswith("  ") and stripped.endswith(":"):
                    # 2-space indent, ends with ':' = new config key name
                    if _config_key and _config_props:
                        _flush_config_entry(_config_key, _config_props, config_schema, md_path)
                    _config_key = stripped[:-1].strip()
                    _config_props = {}
                    continue
                else:
                    # End of config block — flush last entry
                    if _config_key and _config_props:
                        _flush_config_entry(_config_key, _config_props, config_schema, md_path)
                    _config_key = ""
                    _config_props = {}
                    _in_config = False

            if _in_nested_block:
                if line.startswith("  "):
                    continue
                else:
                    _in_nested_block = False

            # ── Regular key: value ──
            if ":" not in stripped:
                continue
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()

            if key in ("expose", "expose_as_tool"):
                result["expose_as_tool"] = val.lower() in ("true", "yes", "1")
            elif key == "triggers":
                triggers = re.findall(r"\w+", val)
                result["triggers"] = triggers if triggers else ["tool_call"]
            elif key == "type":
                stype = val.lower()
                if stype in ("tool", "automation", "hybrid"):
                    result["type"] = stype
                else:
                    log.warning("Unknown skill type '%s' in %s, defaulting to 'tool'", stype, md_path)
            elif key == "tier":
                tier = val.lower()
                if tier in ("lite", "chat", "deep"):
                    result["tier"] = tier
                else:
                    log.warning("Unknown tier '%s' in %s, defaulting to 'chat'", tier, md_path)
            elif key == "multi_turn":
                result["multi_turn"] = val.lower() in ("true", "yes", "1")
            elif key == "requires_config":
                keys = re.findall(r"[A-Z_][A-Z0-9_]+", val)
                result["requires_config"] = keys
            elif key == "core":
                result["core"] = val.lower() in ("true", "yes", "1")
            elif key == "diary":
                tags = re.findall(r"[a-z_][a-z0-9_]*", val)
                result["diary"] = tags
            elif key == "diary_status_order":
                try:
                    result["diary_status_order"] = int(val)
                except ValueError:
                    log.warning("Invalid diary_status_order '%s' in %s", val, md_path)
            elif key == "exclude_transports":
                transports = re.findall(r"[a-z_][a-z0-9_]*", val)
                result["exclude_transports"] = transports
            else:
                result["meta"][key] = val

        # Flush trailing config entry
        if _in_config and _config_key and _config_props:
            _flush_config_entry(_config_key, _config_props, config_schema, md_path)

        # Merge requires_env into requires_config (union)
        if requires_env:
            result["requires_env"] = requires_env
            existing = set(result["requires_config"])
            result["requires_config"] = list(existing | set(requires_env))

        # Store parsed blocks
        if sub_skills:
            result["sub_skills"] = sub_skills
        if config_schema:
            result["config_schema"] = config_schema
        if _nudge_props or _in_nudge:
            awake_raw = _nudge_props.get("requires_awake", "true").lower()
            interval_raw = _nudge_props.get("check_interval", "0")
            result["nudge_meta"] = NudgeMeta(
                requires_awake=awake_raw not in ("false", "no", "0"),
                check_interval_s=float(interval_raw),
            )
        if _writes_diary or _writes_db or _in_writes:
            result["writes_meta"] = WritesMeta(diary=_writes_diary, db=_writes_db)

        # Track sense: block presence
        result["has_sense"] = _has_sense

    # ── Extract usage rules ──
    result["usage_rules"] = _extract_usage_rules(content)

    # ── Extract config schema from ## Config table (fallback if front-matter config: not present) ──
    if not result["config_schema"]:
        result["config_schema"] = _parse_config_schema(content)

    # ── Detect format and parse tools ──
    if re.search(r"^## Tools\s*$", content, re.MULTILINE):
        result["tools"] = _parse_tools_v2(content)
    elif re.search(r"^## Tool:\s*", content, re.MULTILINE):
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


def _parse_config_schema(content: str) -> list[dict]:
    """Extract ## Config section table from SKILL.md.

    Expected format:
      ## Config
      | Key | Type | Secret | Default | Description |
      |-----|------|--------|---------|-------------|
      | MY_API_KEY | string | yes | | API key for service |

    Returns list of dicts: [{key, type, secret, default, description}, ...]
    """
    config_match = re.search(
        r"^## Config\s*\n(.*?)(?=\n## |\Z)", content, re.MULTILINE | re.DOTALL
    )
    if not config_match:
        return []

    section = config_match.group(1)
    schema: list[dict] = []
    rows = re.findall(
        r"\|\s*([A-Z_][A-Z0-9_]*)\s*\|\s*(\w+)\s*\|\s*(yes|no)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|",
        section, re.IGNORECASE,
    )
    for key, ctype, secret, default, desc in rows:
        schema.append({
            "key": key,
            "type": ctype.lower(),
            "secret": secret.lower() == "yes",
            "default": default.strip(),
            "description": desc.strip(),
        })
    return schema


def _extract_tool_annotations(heading: str) -> tuple[str, str, str]:
    """Extract risk level and skill override from a tool heading.

    "tool_name (L0)"              → ("L0", "", "tool_name")
    "tool_name (L1, skill: foo)"  → ("L1", "foo", "tool_name")
    "tool_name"                   → ("L0", "", "tool_name")
    """
    risk_level = "L0"
    skill_override = ""
    paren_match = re.match(r"^(\S+)\s*\(([^)]+)\)", heading)
    if paren_match:
        tool_name = paren_match.group(1)
        annotations = paren_match.group(2)
        for part in annotations.split(","):
            part = part.strip()
            if part.startswith("L") and len(part) >= 2 and part[1:].isdigit():
                risk_level = part
            elif part.startswith("skill:"):
                skill_override = part.split(":", 1)[1].strip()
    else:
        tool_name = heading.split()[0] if heading else ""
    return risk_level, skill_override, tool_name


def _parse_tools_v1(content: str) -> list[dict]:
    """Parse v1 format: ``## Tool: tool_name`` sections."""
    tools = []
    tool_blocks = re.split(r"^## Tool:\s*", content, flags=re.MULTILINE)[1:]
    for block in tool_blocks:
        lines = block.strip().split("\n")
        heading = lines[0].strip()
        # Strip annotations: "tool_name (L0)" → "tool_name"
        risk_level, skill_override, tool_name = _extract_tool_annotations(heading)

        desc = ""
        desc_match = re.search(r"Description[:\s]*(.+?)(?:\n##|\n###|\Z)",
                               block, re.DOTALL | re.IGNORECASE)
        if desc_match:
            desc = desc_match.group(1).strip().split("\n")[0].strip()

        params, required_params = _parse_param_table(block)
        tool_def = _build_tool_schema(tool_name, desc, params, required_params)
        tool_def["_risk_level"] = risk_level
        tool_def["_skill_override"] = skill_override
        tools.append(tool_def)
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
        heading = lines[0].strip()
        # Strip annotations: "send_sticker (L0)" → "send_sticker"
        risk_level, skill_override, tool_name = _extract_tool_annotations(heading)

        if not tool_name:
            continue

        # Description is the first non-empty line after the tool name
        desc = ""
        for line in lines[1:]:
            line = line.strip()
            if line and not line.startswith("|") and not line.startswith("-"):
                desc = line
                break

        params, required_params = _parse_param_table(block)
        tool_def = _build_tool_schema(tool_name, desc, params, required_params)
        tool_def["_risk_level"] = risk_level
        tool_def["_skill_override"] = skill_override
        tools.append(tool_def)
    return tools


def _parse_param_table(block: str) -> tuple[dict, list[str]]:
    """Extract parameters from a markdown table in a tool block.

    Supports:
      - Enum types: ``string (enum: list, restore)``
      - Array types: auto-wrapped with ``items: {type: string}``
      - Required column: ✅, yes, true, Y
    """
    params: dict = {}
    required: list[str] = []

    # Find table rows — flexible pattern that captures full cells
    in_table = False
    table_header_seen = False
    param_rows: list[str] = []

    for line in block.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|") and "Type" in stripped and "Description" in stripped:
            in_table = True
            table_header_seen = False
            continue
        if in_table and stripped.startswith("|") and set(stripped.replace("|", "").strip()) <= {"-", " ", ":"}:
            table_header_seen = True
            continue
        if in_table and stripped.startswith("|") and table_header_seen:
            param_rows.append(stripped)
            continue
        if in_table and not stripped.startswith("|"):
            in_table = False

    for row in param_rows:
        cells = [c.strip() for c in row.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        if len(cells) < 4:
            continue

        pname = cells[0].strip()
        ptype = cells[1].strip()
        preq = cells[2].strip()
        pdesc = cells[3].strip()

        if pname.lower() in ("name", "parameter") or pname.startswith("("):
            continue
        if not ptype:
            continue

        prop: dict = {"type": ptype, "description": pdesc}

        # Handle enum: "string (enum: list, restore)"
        enum_match = re.match(r"(\w+)\s*\(enum:\s*(.+)\)", ptype)
        if enum_match:
            prop["type"] = enum_match.group(1)
            prop["enum"] = [e.strip() for e in enum_match.group(2).split(",")]

        # OpenAI requires array types to have an "items" schema
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}

        params[pname] = prop
        if preq.lower() in ("yes", "true", "y") or preq == "\u2705":
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

    v3 attributes:
        tier        — "lite" | "chat" | "deep" (model routing)
        sub_skills  — additional sub-skill descriptions for pre-router
        config      — resolved config values (DB > env > schema default)
    """

    def __init__(self):
        self._skill_md: dict | None = None
        self._name: str = ""
        self.description: str = ""
        self.skill_type: str = "tool"
        self.tier: str = "chat"
        self.multi_turn: bool = False
        self.usage_rules: str = ""
        self.requires_config: list[str] = []
        self.has_observer: bool = False
        self.diary_tags: list[str] = []
        self.config_schema: list[dict] = []         # backward compat (dict-based)
        self._config_schema_typed: list[ConfigField] = []  # v3 typed schema
        self.sub_skills: dict[str, str] = {}
        self.core: bool = False                       # core skills cannot be disabled
        self.config: dict = {}                       # resolved config values
        self.diary_status_order: int = 50            # diary panel ordering (lower = higher)
        self.exclude_transports: list[str] = []      # transports where this skill is unavailable

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
        """Populate v2/v3 attributes from parsed SKILL.md data."""
        meta = parsed.get("meta", {})
        if not self._name and meta.get("name"):
            self._name = meta["name"]
        if not self.description and meta.get("description"):
            self.description = meta["description"]
        self.skill_type = parsed.get("type", "tool")
        self.tier = parsed.get("tier", "chat")
        self.multi_turn = parsed.get("multi_turn", False)
        self.usage_rules = parsed.get("usage_rules", "")
        self.has_observer = parsed.get("has_sense", False)
        self.core = parsed.get("core", False)
        self.diary_tags = parsed.get("diary", [])
        self.sub_skills = parsed.get("sub_skills", {})
        self.diary_status_order = int(parsed.get("diary_status_order", 50))
        self.exclude_transports = parsed.get("exclude_transports", [])

        # Merge requires_config and requires_env
        rc = set(parsed.get("requires_config", []))
        re_env = set(parsed.get("requires_env", []))
        self.requires_config = list(rc | re_env)

        # Config schema — support both ConfigField list (v3) and dict list (v2)
        raw_schema = parsed.get("config_schema", [])
        if raw_schema and isinstance(raw_schema[0], ConfigField):
            self._config_schema_typed = raw_schema
            # Also populate dict-based for backward compat
            self.config_schema = [
                {"key": f.key, "type": f.type, "secret": False,
                 "default": f.default, "description": f.description,
                 "internal": f.internal}
                for f in raw_schema
            ]
        else:
            self.config_schema = raw_schema
            # Build typed schema from dicts
            self._config_schema_typed = [
                ConfigField(
                    key=d["key"],
                    type=d.get("type", "str"),
                    default=d.get("default", ""),
                    description=d.get("description", ""),
                    internal=bool(d.get("internal", False)),
                )
                for d in raw_schema
                if d.get("key")
            ]

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

    def get_config(self, key: str) -> str:
        """Read a config value with priority: DB override > env > schema default.

        If self.config is populated (by framework during discovery or refresh_config),
        reads from there first. Falls back to inline resolution for backward compat.
        """
        # 1. Resolved config dict (populated by framework)
        if key in self.config:
            return str(self.config[key])

        # 2. DB override (per-skill)
        try:
            from mochi.db import get_skill_config
            db_config = get_skill_config(self.name)
            if key in db_config:
                return db_config[key]
        except Exception:
            pass  # DB not available (e.g., during tests)

        # 3. Environment variable
        env_val = os.getenv(key)
        if env_val is not None:
            return env_val

        # 4. Schema default
        for entry in self._config_schema_typed:
            if entry.key == key and entry.default:
                return entry.default
        for entry in self.config_schema:
            if entry["key"] == key and entry.get("default"):
                return entry["default"]

        return ""

    def refresh_config(self) -> None:
        """Re-resolve config from the priority chain (DB > env > SKILL.md default).

        Builds a new dict then atomically replaces self.config (GIL-safe).
        Called by admin API after DB config changes for hot reload.
        """
        if not self._config_schema_typed:
            return
        from mochi.skill_config_resolver import resolve_skill_config
        self.config = resolve_skill_config(self.name, self._config_schema_typed)

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

    def init_schema(self, conn) -> None:
        """Create DB tables needed by this skill.

        Called once at startup (after init_db, during discover).
        Use CREATE TABLE IF NOT EXISTS only — no destructive DDL.
        The conn is provided by the framework; do NOT close it.
        Use ``ensure_column()`` from ``mochi.db`` for migrations.
        """
        pass

    def diary_status(self, user_id: int, today: str, now: "datetime") -> list[str] | None:
        """Return lines for the 今日状態 diary panel.

        Override in subclasses to contribute status lines.
        Called by collect_diary_status() on every heartbeat tick.

        Args:
            user_id: Owner user ID.
            today: Logical date string (YYYY-MM-DD).
            now: Current datetime (TZ-aware).

        Returns:
            List of markdown lines, or None to opt out.
        """
        return None


# ---------------------------------------------------------------------------
# v3: Metadata-only scanner — reads SKILL.md files without importing handlers
# ---------------------------------------------------------------------------

def scan_skill_metadata(skills_dir: str | None = None) -> list[SkillMeta]:
    """Scan all SKILL.md files and return metadata WITHOUT importing handler modules.

    This is safe to call at module-load time (no circular import risk).
    Only reads .md files via _parse_skill_md().

    Args:
        skills_dir: Path to skills directory. Defaults to mochi/skills/ relative to this file.

    Returns:
        List of SkillMeta for every SKILL.md found.
    """
    if skills_dir is None:
        skills_dir = os.path.dirname(os.path.abspath(__file__))

    result: list[SkillMeta] = []

    for entry in sorted(os.listdir(skills_dir)):
        skill_dir = os.path.join(skills_dir, entry)
        if not os.path.isdir(skill_dir):
            continue
        if entry.startswith("_"):
            continue
        md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(md_path):
            continue

        try:
            parsed = _parse_skill_md(md_path)
        except Exception as e:
            log.warning("scan_skill_metadata: skipping %s: %s", md_path, e)
            continue

        meta_dict = parsed["meta"]
        name = meta_dict.get("name", entry)
        description = meta_dict.get("description", "").strip('"').strip("'")
        skill_type = parsed.get("type", "tool")
        tier = parsed.get("tier", "chat")
        sub_skills = parsed.get("sub_skills", {})

        # Build ToolMeta list from parsed tools
        tools: list[ToolMeta] = []
        for tool_def in parsed.get("tools", []):
            func = tool_def.get("function", {})
            tool_name = func.get("name", "")
            if not tool_name:
                continue
            risk = tool_def.get("_risk_level", "L0")
            override = tool_def.get("_skill_override", "")
            tools.append(ToolMeta(
                name=tool_name,
                skill=override or name,
                risk_level=risk,
            ))

        # Merge requires_config and requires_env
        req = list(set(parsed.get("requires_config", [])) | set(parsed.get("requires_env", [])))

        # Config schema — normalize to ConfigField list
        raw_schema = parsed.get("config_schema", [])
        if raw_schema and isinstance(raw_schema[0], ConfigField):
            config_schema = raw_schema
        else:
            config_schema = [
                ConfigField(key=d["key"], type=d.get("type", "str"),
                            default=d.get("default", ""), description=d.get("description", ""))
                for d in raw_schema if d.get("key")
            ]

        result.append(SkillMeta(
            name=name,
            description=description,
            skill_type=skill_type,
            tier=tier,
            tools=tools,
            sub_skills=sub_skills,
            requires_env=req,
            config_schema=config_schema,
            nudge_meta=parsed.get("nudge_meta"),
            writes_meta=parsed.get("writes_meta"),
            triggers=parsed.get("triggers", []),
            has_sense=parsed.get("has_sense", False),
            exclude_transports=parsed.get("exclude_transports", []),
        ))

    # ── Startup lint validation ──
    for m in result:
        if not m.description:
            log.warning("[SkillLint] %s: missing description — pre-router cannot classify", m.name)
        if m.skill_type in ("tool", "hybrid") and not m.tools:
            log.warning("[SkillLint] %s: type=%s but no tools declared", m.name, m.skill_type)
        if m.skill_type == "automation" and not m.triggers:
            log.warning("[SkillLint] %s: type=automation but no triggers declared", m.name)
        if m.has_sense and not os.path.isfile(os.path.join(skills_dir, m.name, "observer.py")):
            log.warning("[SkillLint] %s: declares sense: but no observer.py found in skill directory", m.name)

    log.info("[SkillMeta] Scanned %d SKILL.md files from %s", len(result), skills_dir)
    return result


def _has_missing_env(requires_env: list[str], skill_name: str = "") -> bool:
    """Check if any required env vars are missing (env OR DB config)."""
    if not requires_env:
        return False
    db_cfg: dict = {}
    if skill_name:
        try:
            from mochi.db import get_skill_config
            db_cfg = get_skill_config(skill_name)
        except Exception:
            pass
    return any(not os.getenv(k) and not db_cfg.get(k) for k in requires_env)


def build_skill_descriptions(metas: list[SkillMeta],
                             transport: str = "") -> dict[str, str]:
    """Build {skill_name: description} for pre-router catalog.

    Includes only type=tool/hybrid skills that have at least one tool.
    Excludes skills whose required env vars are missing (auto-disabled).
    Excludes skills incompatible with the given transport.
    Also includes sub_skills as separate entries.
    """
    result: dict[str, str] = {}
    for m in metas:
        if m.skill_type not in ("tool", "hybrid"):
            continue
        if not m.tools:
            continue
        if _has_missing_env(m.requires_env, m.name):
            continue
        if transport and transport in m.exclude_transports:
            continue
        result[m.name] = m.description
        for sub_name, sub_val in m.sub_skills.items():
            desc = sub_val.split("|")[0].strip()
            result[sub_name] = desc
    return result


def build_tool_metadata(metas: list[SkillMeta]) -> dict[str, dict]:
    """Build {tool_name: {skill, risk_level}} from parsed SKILL.md tools.

    Excludes tools from skills whose required env vars are missing.
    Includes the virtual request_tools entry.
    """
    result: dict[str, dict] = {}
    for m in metas:
        if _has_missing_env(m.requires_env, m.name):
            continue
        for tool in m.tools:
            result[tool.name] = {
                "skill": tool.skill,
                "risk_level": tool.risk_level,
            }
    result["request_tools"] = {"skill": "_virtual", "risk_level": "L0"}
    return result


def build_tier_defaults(metas: list[SkillMeta]) -> dict[str, str]:
    """Build {skill_name: tier} for skills with non-default tier.

    Only includes skills where tier != "chat" (chat is the default).
    Excludes skills whose required env vars are missing.
    Sub-skills inherit parent tier unless they have |tier:xxx suffix.
    """
    result: dict[str, str] = {}
    for m in metas:
        if m.skill_type not in ("tool", "hybrid"):
            continue
        if _has_missing_env(m.requires_env, m.name):
            continue
        if m.tier != "chat":
            result[m.name] = m.tier
        for sub_name, sub_val in m.sub_skills.items():
            parts = sub_val.split("|")
            sub_tier = None
            for part in parts[1:]:
                part = part.strip()
                if part.startswith("tier:"):
                    sub_tier = part.split(":", 1)[1].strip()
            if sub_tier and sub_tier != "chat":
                result[sub_name] = sub_tier
            elif sub_tier is None and m.tier != "chat":
                result[sub_name] = m.tier
    return result
