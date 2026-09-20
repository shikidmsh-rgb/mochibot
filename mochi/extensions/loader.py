"""Load trusted ordinary tool packages without importing Mochi's runtime services."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import uuid
import weakref

from mochi.skills.base import Skill, VALID_TOOL_LOADS, _parse_skill_md

from . import store
from .store import ExtensionError

_BASE_METHODS = (
    "init_schema", "diary_status", "run", "get_tools", "available_tools", "skill_md",
    "_populate_from_md", "name", "triggers", "tool_names", "handles", "__new__",
)
_UNSUPPORTED_HOOKS = ("prompt_section", "observe", "observer", "Observer")
_SCALARS = {"string", "integer", "number", "boolean"}
SUPPORTED_MOD_APIS = (1,)
log = logging.getLogger(__name__)


def _mod_api(content: str) -> dict:
    front = re.match(r"^---\s*\n(.*?)\n---", content, re.S)
    declarations = []
    block = ""
    # Match the existing front-matter block rules, including indented metadata,
    # without parsing tool schemas that a future API may define differently.
    for line in front.group(1).strip().splitlines() if front else []:
        stripped = line.strip()
        if stripped in {"config:", "requires:", "sense:", "sub_skills:"}:
            block = stripped[:-1]
            continue
        if block:
            nested = (
                (line.startswith("    ") and ":" in stripped)
                or (line.startswith("  ") and stripped.endswith(":"))
            ) if block == "config" else (
                line.startswith("  ") and (block == "sense" or ":" in stripped)
            )
            if nested:
                continue
            block = ""
        key, separator, value = stripped.partition(":")
        if separator and key.strip() == "mod_api":
            declarations.append(value.strip())
    if not declarations:
        return {"declared": None, "effective": 1, "status": "legacy"}
    invalid = {
        "declared": None, "effective": None, "status": "invalid",
        "error": "mod_api must be declared once as a positive decimal integer.",
    }
    if len(declarations) != 1:
        return invalid
    value = declarations[0].strip()
    if not re.fullmatch(r"[1-9][0-9]*", value):
        return invalid
    try:
        version = int(value)
    except ValueError:
        return invalid
    return {
        "declared": version, "effective": version,
        "status": "supported" if version in SUPPORTED_MOD_APIS else "unsupported",
    }


def inspect_mod_api(package_dir: Path) -> dict:
    """Inspect only the bounded declaration, including incompatible packages."""
    package_dir = store._safe_path(package_dir)
    content = store._read_bytes(package_dir / "SKILL.md").decode("utf-8")
    return _mod_api(content)


def _require_mod_api(content: str) -> None:
    info = _mod_api(content)
    if info["status"] == "invalid":
        raise ExtensionError("invalid_mod_api", info["error"])
    if info["status"] == "unsupported":
        raise ExtensionError(
            "unsupported_mod_api",
            f"Mod API {info['declared']} is not supported; this Base supports "
            f"{', '.join(str(version) for version in SUPPORTED_MOD_APIS)}. "
            "Adapt the code to a supported contract or use a compatible Base; "
            "changing the version label alone does not adapt the code.",
        )


def _validate_tools(extension_id: str, tools: list[dict]) -> None:
    if not tools:
        raise ExtensionError("invalid_metadata", "An extension must declare at least one ordinary tool.")
    names = set()
    registry = sys.modules.get("mochi.skills")
    registered = getattr(registry, "_tool_map", {})
    for tool in tools:
        function = tool.get("function", {})
        name = function.get("name", "")
        if (
            not isinstance(name, str) or len(name) > 64
            or not name.startswith(extension_id + "_")
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or name == extension_id + "_"
        ):
            raise ExtensionError("invalid_tool_name", f"Tool names must start with {extension_id}_ and be at most 64 characters: {name}")
        if name in names or (name in registered and registered[name] != extension_id):
            raise ExtensionError("tool_collision", f"Tool name is already owned: {name}")
        names.add(name)
        if tool.get("type") != "function" or tool.get("_load") not in VALID_TOOL_LOADS:
            raise ExtensionError("invalid_schema", f"{name} must declare a valid ordinary tool load.")
        schema = function.get("parameters", {})
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            schema.get("type") != "object" or not isinstance(properties, dict)
            or not isinstance(required, list) or len(required) != len(set(required))
            or any(key not in properties for key in required)
        ):
            raise ExtensionError("invalid_schema", f"{name} has an invalid parameter schema.")
        for key, prop in properties.items():
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key) or not isinstance(prop, dict):
                raise ExtensionError("invalid_schema", f"{name} has an invalid parameter name.")
            kind = prop.get("type")
            if kind not in _SCALARS and not (kind == "array" and prop.get("items") == {"type": "string"}):
                raise ExtensionError("invalid_schema", f"{name}.{key}: only scalar and string-array parameters are supported.")
            if "enum" in prop and (
                kind != "string" or not isinstance(prop["enum"], list)
                or not prop["enum"] or any(not isinstance(value, str) for value in prop["enum"])
            ):
                raise ExtensionError("invalid_schema", f"{name}.{key}: Markdown enums must contain strings.")


def _validate_parameter_rows(content: str) -> None:
    """Reject malformed rows that the intentionally lenient built-in parser skips."""
    section = re.search(r"^## Tools\s*\n(.*?)(?=\n## |\Z)", content, re.M | re.S)
    if not section:
        return
    for block in re.split(r"^### ", section.group(1), flags=re.M)[1:]:
        seen = set()
        in_table = False
        for line in block.splitlines()[1:]:
            line = line.strip()
            if not line.startswith("|"):
                in_table = False
                continue
            if "Type" in line and "Description" in line:
                in_table = True
                continue
            if set(line.replace("|", "").strip()) <= {"-", " ", ":"}:
                continue
            if not in_table:
                raise ExtensionError("invalid_schema", "Tool tables need the standard Parameter/Type/Required/Description header.")
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) != 4 or not cells[0] or not cells[1]:
                raise ExtensionError("invalid_schema", "Each parameter row must have four nonempty name/type columns.")
            if cells[0] in seen:
                raise ExtensionError("invalid_schema", f"Duplicate parameter: {cells[0]}")
            seen.add(cells[0])
            if cells[2].lower() not in {"yes", "true", "y", "no", "false", "n", "✅", "❌", ""}:
                raise ExtensionError("invalid_schema", f"Invalid Required value for parameter {cells[0]}.")
            if not re.fullmatch(r"(?:string|integer|number|boolean|array|array\s*\(items:\s*string\)|string\s*\(enum:\s*[^)]+\))", cells[1]):
                raise ExtensionError("invalid_schema", f"Unsupported Markdown parameter type: {cells[1]}")


def validate_package(extension_id: str, package_dir: Path) -> dict:
    """Check package files, syntax, and Markdown metadata without importing its code."""
    store.validate_id(extension_id)
    package_dir = store._safe_path(package_dir)
    files = store.inspect_package(package_dir)
    names = {item["path"] for item in files}
    missing = {"__init__.py", "handler.py", "SKILL.md"} - names
    if missing:
        raise ExtensionError("incomplete_package", f"Missing required files: {', '.join(sorted(missing))}")
    parsed = read_metadata(extension_id, package_dir)
    for item in files:
        if item["path"].endswith(".py"):
            try:
                compile(
                    store._read_bytes(store._file_path(package_dir, item["path"])),
                    item["path"], "exec", dont_inherit=True,
                )
            except (SyntaxError, ValueError) as exc:
                raise ExtensionError("invalid_python", f"Invalid Python in {item['path']}: {exc}") from exc
    return parsed


def read_metadata(extension_id: str, package_dir: Path) -> dict:
    """Read bounded Markdown for management, even when the handler is broken.

    Returns the unchanged ``_parse_skill_md`` shape: ``meta``, ``tools``,
    ``config_schema`` (ConfigField objects or legacy dicts), ``requires_config``,
    ``requires_env``, ``triggers``, and the usual capability/type flags.
    Applies the same metadata/schema checks used by publish, without importing
    or compiling the handler; a disabled package can still expose its config.
    """
    store.validate_id(extension_id)
    package_dir = store._safe_path(package_dir)
    content = store._read_bytes(package_dir / "SKILL.md").decode("utf-8")
    front = re.match(r"^---\s*\n(.*?)\n---", content, re.S)
    if not front:
        raise ExtensionError("invalid_metadata", "SKILL.md requires Markdown front matter with the extension name.")
    _require_mod_api(content)
    metadata = {}
    for key, value in re.findall(r"^([a-z_]+):\s*([^\n]*)", front.group(1), re.M):
        if key in metadata:
            raise ExtensionError("invalid_metadata", f"Duplicate metadata key: {key}")
        metadata[key] = value.strip()
    if metadata.get("name") != extension_id:
        raise ExtensionError("invalid_metadata", f"SKILL.md name must equal {extension_id}.")
    if metadata.get("type", "tool") != "tool":
        raise ExtensionError("unsupported_hooks", "Personal extensions support type: tool only.")
    if any(key in metadata for key in ("sense", "observer", "prompt_section", "init_schema", "diary_status", "diary_status_order")):
        raise ExtensionError("unsupported_hooks", "Observers, shared schema, prompt and diary hooks are not supported.")
    _validate_parameter_rows(content)
    try:
        parsed = _parse_skill_md(str(package_dir / "SKILL.md"))
    except ValueError as exc:
        raise ExtensionError("invalid_metadata", str(exc)) from exc
    if (
        parsed["meta"].get("name") != extension_id or parsed["type"] != "tool"
        or parsed["triggers"] != ["tool_call"] or parsed["locked"]
        or parsed["has_sense"] or parsed["diary"] or parsed["sub_skills"]
    ):
        raise ExtensionError("unsupported_hooks", "Extensions must be unlocked ordinary tools with only tool_call triggers and no diary/observer/sub-skill hooks.")
    declared_config = {
        field["key"] if isinstance(field, dict) else field.key
        for field in parsed["config_schema"]
    }
    missing_schema = set(parsed["requires_config"]) - declared_config
    if missing_schema:
        raise ExtensionError(
            "invalid_config",
            f"Required configuration needs a config declaration: {', '.join(sorted(missing_schema))}",
        )
    _validate_tools(extension_id, parsed["tools"])
    return parsed


def _validate_class(skill_cls: type[Skill]) -> None:
    for name in _BASE_METHODS:
        if inspect.getattr_static(skill_cls, name) is not inspect.getattr_static(Skill, name):
            raise ExtensionError("unsupported_hooks", f"External skills cannot override {name}.")
    if any(hasattr(skill_cls, name) for name in _UNSUPPORTED_HOOKS) or any(
        base.__module__.startswith("mochi.observers") for base in skill_cls.__mro__
    ):
        raise ExtensionError("unsupported_hooks", "Observer and prompt hooks are not supported.")
    if not inspect.iscoroutinefunction(skill_cls.execute):
        raise ExtensionError("invalid_handler", "Skill.execute must be an async method.")


def _clear_namespace(namespace: str) -> None:
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(namespace + "."):
            sys.modules.pop(name, None)


def _release_snapshot(workspace: TemporaryDirectory, namespace: str | None) -> None:
    if namespace is not None:
        _clear_namespace(namespace)
    try:
        workspace.cleanup()
    except Exception:
        log.warning("Could not clean extension runtime snapshot %s", workspace.name, exc_info=True)


def load_snapshot(
    extension_id: str, package_dir: Path, *, data_dir: Path, config: dict | None = None,
) -> Skill:
    """Load a private code copy kept unchanged for this Skill's entire lifetime.

    Late relative imports remain valid across draft edits and current/previous
    renames. Strong references to the Skill retain its files and module namespace;
    GC (or normal shutdown) releases only this snapshot, never other live copies.
    """
    root = store.extension_root(extension_id)
    root.mkdir(parents=True, exist_ok=True)
    workspace = TemporaryDirectory(prefix="_loaded_", dir=root)
    namespace = None
    try:
        snapshot = store._safe_path(Path(workspace.name)) / "package"
        store.copy_package(package_dir, snapshot)
        skill = load_from_path(extension_id, snapshot, data_dir=data_dir, config=config)
        namespace = type(skill).__module__.rpartition(".")[0]
        weakref.finalize(skill, _release_snapshot, workspace, namespace)
        return skill
    except BaseException:
        _release_snapshot(workspace, namespace)
        raise


def load_from_path(
    extension_id: str, package_dir: Path, *, data_dir: Path, config: dict | None = None,
) -> Skill:
    """Return one owned Skill loaded in its own namespace with relative imports.

    The instance has ``external=True``, the actual ``__module_file__``,
    ``data_dir`` as an absolute Path, and a copy of explicit ``config`` (or {}).
    It already has parsed Markdown attributes; callers resolve production config
    and register the complete tool map separately.
    """
    package_dir = store._safe_path(package_dir)
    parsed = validate_package(extension_id, package_dir)
    data_dir = store._safe_path(data_dir)
    if data_dir.exists() and not data_dir.is_dir():
        raise ExtensionError("invalid_path", "Extension data_dir must be a directory.")
    if config is not None and not isinstance(config, dict):
        raise ExtensionError("invalid_config", "Extension config must be a dictionary.")
    path_key = hashlib.sha256(str(package_dir).encode()).hexdigest()[:16]
    namespace = f"_mochi_extension_{extension_id}_{path_key}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        namespace, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ExtensionError("invalid_package", "Could not create an extension package loader.")
    try:
        package = importlib.util.module_from_spec(spec)
        sys.modules[namespace] = package
        spec.loader.exec_module(package)
        handler = importlib.import_module(f"{namespace}.handler")
        candidates = {
            candidate for candidate in vars(handler).values()
            if isinstance(candidate, type) and candidate is not Skill
            and issubclass(candidate, Skill) and candidate.__module__ == handler.__name__
            and not inspect.isabstract(candidate)
        }
        if len(candidates) != 1:
            raise ExtensionError("invalid_handler", "handler.py must own exactly one concrete Skill subclass.")
        skill_cls = candidates.pop()
        _validate_class(skill_cls)
        # Constructors that consult skill_md must see the actual package path.
        skill_cls.__module_file__ = str(package_dir / "handler.py")
        data_dir.mkdir(parents=True, exist_ok=True)
        skill = object.__new__(skill_cls)
        skill.__module_file__ = str(package_dir / "handler.py")
        skill.external = True
        skill.data_dir = data_dir
        skill.config = dict(config or {})
        skill_cls.__init__(skill)
        _ = skill.skill_md
        if skill.name != extension_id or skill.skill_md != parsed or skill.get_tools() != parsed["tools"]:
            raise ExtensionError("invalid_handler", "Handler metadata must come unchanged from this package's SKILL.md.")
        for name in (*_BASE_METHODS, *_UNSUPPORTED_HOOKS):
            if name in vars(skill):
                raise ExtensionError("unsupported_hooks", f"External skills cannot replace {name} on the instance.")
        if skill.skill_type != "tool" or skill.has_observer or skill.diary_tags or skill.locked or skill.sub_skills:
            raise ExtensionError("unsupported_hooks", "Handler cannot enable lifecycle hooks outside its ordinary tool metadata.")
        skill._mod_api = inspect_mod_api(package_dir)
        return skill
    except BaseException:
        _clear_namespace(namespace)
        raise
