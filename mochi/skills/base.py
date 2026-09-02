"""Skill base class and SKILL.md parser.

Tools use ``## Tools`` / ``### tool_name (load)`` sections, where load is
``resident``, ``routed``, or ``on_demand``.

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

log = logging.getLogger(__name__)

_VALID_MODEL_TIERS = frozenset({"lite", "main"})
_LEGACY_MODEL_TIERS = frozenset({"chat", "deep"})
_WARNED_INVALID_TIERS: set[tuple[str, str]] = set()
VALID_TOOL_LOADS = frozenset({"resident", "routed", "on_demand"})


def _normalize_model_tier(tier: str, source: str) -> str:
    normalized = tier.strip().lower()
    if normalized in _VALID_MODEL_TIERS:
        return normalized
    warning_key = (source, normalized)
    if warning_key not in _WARNED_INVALID_TIERS:
        _WARNED_INVALID_TIERS.add(warning_key)
        if normalized in _LEGACY_MODEL_TIERS:
            log.warning(
                "Legacy tier '%s' in %s mapped to 'main'",
                normalized,
                source,
            )
        else:
            log.warning(
                "Unknown tier '%s' in %s, defaulting to 'main'",
                normalized,
                source,
            )
    return "main"


def _normalize_sub_skill_value(value: str, md_path: str, sub_name: str) -> str:
    parts = value.split("|")
    for index, part in enumerate(parts[1:], start=1):
        key, separator, tier = part.strip().partition(":")
        if separator and key.strip() == "tier":
            normalized = _normalize_model_tier(
                tier,
                f"{md_path} sub-skill '{sub_name}'",
            )
            parts[index] = f"tier:{normalized}"
    return "|".join(parts)


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
    actor: str = ""          # "main" only when invoked by a Main tool loop
    tool_name: str = ""     # only set for trigger="tool_call"
    args: dict = field(default_factory=dict)
    observation: dict | None = None  # only set for trigger="heartbeat"


@dataclass
class SkillResult:
    """Unified result returned by Skill.run().

    - output: text string (fed back to LLM or logged)
    - actions: heartbeat-style action list [{"type": "message", "content": ...}]
    - success: whether the skill executed without error
    - summary: deterministic cross-turn receipt (never LLM-generated)
    - entity_refs: stable references such as reminder:27
    - state_changed: whether this call changed durable user state
    - error_code/retryable: optional machine-readable failure facts
    - execution_started: whether the skill handler was entered
    - state_change_unknown: an exception may have happened after a side effect
    - content_source: optional provenance for content returned to the model
    """
    output: str = ""
    actions: list[dict] = field(default_factory=list)
    success: bool = True
    summary: str = ""
    entity_refs: list[str] = field(default_factory=list)
    state_changed: bool = False
    error_code: str = ""
    retryable: bool | None = None
    execution_started: bool = False
    state_change_unknown: bool = False
    content_source: str = ""


@dataclass
class ConfigField:
    """One declared config key for a skill (parsed from SKILL.md config: block)."""
    key: str
    type: str           # "int", "float", "bool", "str"
    default: str        # always str — cast by resolver
    description: str = ""
    secret: bool = False
    internal: bool = False  # hidden from admin UI when True


# ---------------------------------------------------------------------------
# SKILL.md Parsing
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
        secret=props.get("secret", "").lower() in ("true", "yes", "1"),
        internal=props.get("internal", "").lower() in ("true", "yes", "1"),
    ))


def _parse_skill_md(md_path: str) -> dict:
    """Parse a SKILL.md file into framework metadata and tool definitions.

    Front-matter supports multi-line blocks such as sub_skills, requires,
    config, nudge, writes, and sense.
    """
    result: dict = {
        "meta": {},
        "tools": [],
        "triggers": ["tool_call"],
        "capability_context": "",
        "type": "tool",
        "multi_turn": False,
        "requires_config": [],
        "requires_env": [],
        "has_sense": False,
        "locked": False,
        "diary": [],
        "diary_status_order": 50,
        "config_schema": [],
        "sub_skills": {},
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
        _in_nested_block = False  # for blocks we skip (sense:, etc.)
        _has_sense = False        # track if sense: block is present

        # Accumulators for multi-line blocks
        _config_key = ""
        _config_props: dict[str, str] = {}
        config_schema: list[ConfigField] = []
        sub_skills: dict[str, str] = {}
        requires_env: list[str] = []

        for line in fm_match.group(1).strip().split("\n"):
            stripped = line.strip()

            # ── Detect block starts ──

            if stripped == "sub_skills:" or stripped.startswith("sub_skills:"):
                inline = stripped.split(":", 1)[1].strip()
                if not inline:
                    _in_sub_skills = True
                    _in_requires = _in_config = _in_nested_block = False
                    continue

            if stripped == "requires:" or (stripped.startswith("requires:") and not stripped.split(":", 1)[1].strip()):
                _in_requires = True
                _in_sub_skills = _in_config = _in_nested_block = False
                continue

            if stripped == "sense:" or (stripped.startswith("sense:") and not stripped.split(":", 1)[1].strip()):
                _in_nested_block = True
                _has_sense = True
                _in_sub_skills = _in_requires = _in_config = False
                continue

            if stripped == "config:" or (stripped.startswith("config:") and not stripped.split(":", 1)[1].strip()):
                _in_config = True
                _in_sub_skills = _in_requires = _in_nested_block = False
                _config_key = ""
                _config_props = {}
                continue

            # ── Parse indented block entries ──

            if _in_sub_skills:
                if line.startswith("  ") and ":" in stripped:
                    sk, sv = stripped.split(":", 1)
                    sub_name = sk.strip()
                    sub_value = sv.strip().strip('"').strip("'")
                    sub_skills[sub_name] = _normalize_sub_skill_value(
                        sub_value,
                        md_path,
                        sub_name,
                    )
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

            if key == "triggers":
                triggers = re.findall(r"\w+", val)
                result["triggers"] = triggers if triggers else ["tool_call"]
            elif key == "type":
                stype = val.lower()
                if stype in ("tool", "automation", "hybrid"):
                    result["type"] = stype
                else:
                    log.warning("Unknown skill type '%s' in %s, defaulting to 'tool'", stype, md_path)
            elif key == "multi_turn":
                result["multi_turn"] = val.lower() in ("true", "yes", "1")
            elif key == "requires_config":
                keys = re.findall(r"[A-Z_][A-Z0-9_]+", val)
                result["requires_config"] = keys
            elif key == "locked":
                result["locked"] = val.lower() in ("true", "yes", "1")
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

        # Track sense: block presence
        result["has_sense"] = _has_sense

    # Only capability facts enter Main. Legacy directive sections such as
    # Usage Rules and Behavior Rules are deliberately not prompt contracts.
    result["capability_context"] = _extract_capability_context(content)

    # ── Extract config schema from ## Config table (fallback if front-matter config: not present) ──
    if not result["config_schema"]:
        result["config_schema"] = _parse_config_schema(content)

    # ── Parse tools ──
    try:
        if re.search(r"^## Tools\s*$", content, re.MULTILINE):
            result["tools"] = _parse_tools_v2(content)
    except ValueError as exc:
        raise ValueError(f"{exc} in {md_path}") from exc

    _validate_tool_loads(result, md_path)

    return result


def _validate_tool_loads(parsed: dict, md_path: str) -> None:
    """Require one explicit current load for every unique tool."""
    seen: set[str] = set()
    for tool in parsed.get("tools", []):
        name = tool.get("function", {}).get("name", "")
        if name in seen:
            raise ValueError(f"Duplicate tool name '{name}' in {md_path}")
        seen.add(name)

        load = tool.get("_load")
        if load not in VALID_TOOL_LOADS:
            raise ValueError(
                f"Tool '{name}' in {md_path} must declare exactly one load: "
                "resident, routed, or on_demand"
            )


def _extract_capability_context(content: str) -> str:
    """Extract the Agent First capability contract from SKILL.md."""
    match = re.search(
        r"^## Capability Context\s*\n(.*?)(?=\n## |\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


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


def _extract_tool_load(heading: str) -> tuple[str, str | None]:
    """Extract one current load annotation from a tool heading."""
    match = re.fullmatch(r"(\S+)\s*\(([^)]+)\)\s*", heading)
    if not match:
        return (heading.split()[0] if heading else ""), None
    tool_name, load = match.group(1), match.group(2).strip()
    if load not in VALID_TOOL_LOADS:
        raise ValueError(f"Invalid tool load '{load}' for '{tool_name}'")
    return tool_name, load


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
        try:
            tool_name, load = _extract_tool_load(heading)
        except ValueError as exc:
            raise ValueError(f"{exc} in SKILL.md tool heading '{heading}'") from exc

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
        tool_def["_load"] = load
        tools.append(tool_def)
    return tools


def _parse_param_table(block: str) -> tuple[dict, list[str]]:
    """Extract parameters from a markdown table in a tool block.

    Supports:
      - Enum types: ``string (enum: list, restore)``
      - Array types: string items by default, or ``array (items: object)``
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

        array_items_match = re.match(r"array\s*\(items:\s*(\w+)\)", ptype)
        if array_items_match:
            prop["type"] = "array"
            prop["items"] = {"type": array_items_match.group(1)}

        # OpenAI requires array types to have an "items" schema
        if prop["type"] == "array" and "items" not in prop:
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
        capability_context — Main-facing capability facts from SKILL.md
        description — human-readable description

    Additional attributes:
        sub_skills  — additional sub-skill descriptions for pre-router
        config      — resolved config values (DB > env > schema default)
    """

    def __init__(self):
        self._skill_md: dict | None = None
        self._name: str = ""
        self.description: str = ""
        self.skill_type: str = "tool"
        self.multi_turn: bool = False
        self.capability_context: str = ""
        self.requires_config: list[str] = []
        self.has_observer: bool = False
        self.diary_tags: list[str] = []
        self.config_schema: list[dict] = []         # backward compat (dict-based)
        self._config_schema_typed: list[ConfigField] = []  # v3 typed schema
        self.sub_skills: dict[str, str] = {}
        self.locked: bool = False                     # cannot be disabled
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
        self.multi_turn = parsed.get("multi_turn", False)
        self.capability_context = parsed.get("capability_context", "")
        self.has_observer = parsed.get("has_sense", False)
        self.locked = parsed.get("locked", False)
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
                {"key": f.key, "type": f.type, "secret": f.secret,
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
                    secret=bool(d.get("secret", False)),
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
    def triggers(self) -> list:
        """How this skill can be invoked: tool_call, heartbeat, cron, slash."""
        return self.skill_md.get("triggers", ["tool_call"])

    def tool_names(self) -> set[str]:
        """Return set of tool names this skill exposes."""
        return {t["function"]["name"] for t in self.get_tools()}

    def handles(self, tool_name: str) -> bool:
        """Check if this skill handles a specific tool name."""
        return tool_name in self.tool_names()

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
            result.execution_started = True
        except Exception as e:
            log.error("Skill %s failed: %s", self.name, e, exc_info=True)
            return SkillResult(
                output=f"Skill error: {e}",
                success=False,
                error_code="skill_exception",
                retryable=False,
                execution_started=True,
                state_change_unknown=True,
            )

        if result.success and type(self).diary_status is not Skill.diary_status:
            try:
                from mochi.diary import refresh_diary_status
                refresh_diary_status(context.user_id or None)
            except Exception as e:
                log.warning("post-skill diary refresh failed for %s: %s", self.name, e)
        return result

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
