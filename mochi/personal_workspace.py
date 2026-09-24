"""Explicit document/source addresses and the existing development preference."""

from __future__ import annotations

from dataclasses import dataclass

from mochi import mochi_files_store as documents
from mochi.extensions import store

MAX_READ_CHARS = min(documents.MAX_READ_CHARS, store.MAX_READ_CHARS)
MAX_READ_FILES = store.MAX_READ_FILES


def file_scope_available(kind: str, *, write: bool = False) -> bool:
    from mochi.tool_policy import filter_tools

    legacy = {
        "documents": ("browse_mochi_files", "save_mochi_file"),
        "extensions": ("inspect_extension", "write_extension"),
    }[kind][int(write)]
    return bool(filter_tools([{"function": {"name": legacy}}]))


def require_file_scope(kind: str, *, write: bool = False) -> None:
    if not file_scope_available(kind, write=write):
        operation = "writing" if write else "browsing"
        raise store.ExtensionError("tool_denied", f"Personal {kind} {operation} is disabled by the configured legacy tool policy.")


def development_enabled() -> bool:
    from mochi.db import get_disabled_skills

    return "development" not in get_disabled_skills()


def set_development_enabled(enabled: bool) -> bool:
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    from mochi.db import set_skill_enabled

    changed = development_enabled() != enabled
    if changed:
        set_skill_enabled("development", enabled)
    return changed


def require_development() -> None:
    if not development_enabled():
        raise store.ExtensionError(
            "development_disabled",
            "Personal development is disabled; documents and read-only source/status remain available.",
        )


@dataclass(frozen=True)
class Address:
    path: str
    kind: str
    name: str = ""
    area: str = ""
    relative: str = ""

    @property
    def draft_path(self) -> str:
        return f"extensions/{self.name}/draft"


def address(path: str) -> Address:
    if not isinstance(path, str) or not path or len(path) > 500 or "\\" in path:
        raise documents.InvalidPathError("path must be a workspace-relative '/' address of at most 500 characters")
    parts = path.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        raise documents.InvalidPathError("empty, hidden, or traversal path components are not allowed")
    if parts[0] == "documents":
        relative = "/".join(parts[1:])
        if relative:
            documents._validate_components(relative, require_markdown=False)
        return Address(path, "documents", relative=relative)
    if parts[0] != "extensions":
        raise documents.InvalidPathError("Only documents and extensions are workspace areas")
    if len(parts) == 1:
        return Address(path, "extensions")
    store.validate_id(parts[1])
    if len(parts) == 2:
        return Address(path, "extensions", name=parts[1])
    if parts[2] not in {"draft", "current", "previous"}:
        raise documents.InvalidPathError("Only draft, current, and previous source areas are accessible")
    relative = "/".join(parts[3:])
    if relative:
        store._relative(relative)
    return Address(path, "extensions", parts[1], parts[2], relative)


def draft_address(path: str) -> Address:
    target = address(path)
    if target.kind != "extensions" or target.area != "draft" or target.relative:
        raise documents.InvalidPathError("Expected a draft root such as extensions/local_reading/draft")
    return target


def page(offset: int, limit: int, maximum: int) -> None:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= maximum:
        raise documents.InvalidArgumentsError(f"offset must be nonnegative and limit must be an integer from 1 to {maximum}")


def _canonical_page(payload: dict, prefix: str, area: str) -> dict:
    for key in ("files", "matches"):
        for item in payload.get(key, []):
            item["path"] = f"{prefix}/{item['path']}"
            item["source_area"] = area
    if "skipped_non_text_paths" in payload:
        payload["skipped_non_text_paths"] = [
            f"{prefix}/{path}" for path in payload["skipped_non_text_paths"]
        ]
    payload["truncated"] = not payload["complete"]
    return payload


def _packages(name: str = "") -> list[dict]:
    from mochi.skills import get_skill_info_all

    fields = {
        "name", "description", "type", "tools", "enabled", "admin_disabled",
        "auto_disabled", "loaded", "load_error", "activation_required",
        "has_draft", "installed", "has_previous", "error", "publish_error",
        "publish_cleanup_error", "config_missing", "mod_api",
    }
    result = []
    for info in get_skill_info_all():
        if info.get("source") != "personal" or (name and info["name"] != name):
            continue
        item = {
            key: value[:1000] if isinstance(value, str) else value
            for key, value in info.items() if key in fields
        }
        item["path"] = f"extensions/{info['name']}"
        item["draft_path"] = f"{item['path']}/draft"
        item["source_paths"] = [
            f"{item['path']}/{area}" for area, present in (
                ("draft", info.get("has_draft")),
                ("current", info.get("installed")),
                ("previous", info.get("has_previous")),
            ) if present
        ]
        result.append(item)
    return sorted(result, key=lambda item: item["name"])


def list_workspace(path: str | None = None, *, offset: int = 0, limit: int = 100) -> dict:
    page(offset, limit, documents.MAX_LIST_RESULTS)
    target = address(path) if path is not None else None
    if target:
        require_file_scope(target.kind)
    if target is None or (target.kind == "extensions" and not target.area):
        source_available = file_scope_available("extensions")
        packages = _packages(target.name if target else "") if source_available else []
        if target and target.name and not packages:
            raise store.ExtensionError("not_found", "Personal package does not exist.")
        entries = packages[offset:offset + limit]
        end = offset + len(entries)
        enabled = development_enabled()
        areas = []
        for kind in ("documents", "extensions"):
            available = file_scope_available(kind)
            areas.append({
                "path": kind, "kind": "documents" if kind == "documents" else "source",
                "available": available,
                "write_available": file_scope_available(kind, write=True) and (kind == "documents" or enabled),
                **({"reason": "tool_denied"} if not available else {}),
            })
        return {
            "action": "list", "scope": path, "development_enabled": enabled,
            "areas": areas, "packages_available": source_available,
            "packages": entries, "offset": offset, "count": len(entries), "total": len(packages),
            "complete": end >= len(packages), "truncated": end < len(packages),
            "next_offset": end if end < len(packages) else None,
        }
    if target.kind == "documents":
        payload = documents.list_files(path=target.relative or None, offset=offset, limit=limit)
        prefix, area = "documents", "documents"
    else:
        payload = store.list_files(
            target.name, area=target.area, path=target.relative or None, offset=offset, limit=limit,
        )
        prefix, area = f"extensions/{target.name}/{target.area}", target.area
    return {**_canonical_page(payload, prefix, area), "action": "list", "scope": path}


def search_workspace(path: str, query: str, *, offset: int = 0, limit: int = 20) -> dict:
    target = address(path)
    require_file_scope(target.kind)
    page(offset, limit, documents.MAX_SEARCH_RESULTS)
    if target.kind == "documents":
        payload = documents.search_files(query, path=target.relative or None, offset=offset, limit=limit)
        prefix, area = "documents", "documents"
    else:
        if not target.area:
            raise documents.InvalidPathError("Source search requires an explicit draft/current/previous area")
        payload = store.search_files(
            target.name, query, area=target.area, path=target.relative or None, offset=offset, limit=limit,
        )
        prefix, area = f"extensions/{target.name}/{target.area}", target.area
    return {**_canonical_page(payload, prefix, area), "action": "search", "scope": path}


def read_workspace(paths: list[str], *, offset: int = 0, limit: int = MAX_READ_CHARS) -> dict:
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_READ_FILES:
        raise documents.InvalidArgumentsError(f"read requires 1 to {MAX_READ_FILES} paths")
    page(offset, limit, MAX_READ_CHARS)
    targets = [address(path) for path in paths]
    for target in targets:
        require_file_scope(target.kind)
        if not target.relative:
            raise documents.InvalidPathError("read paths must name files")
        if target.kind == "documents":
            documents._file_parts(target.relative)
    files, budget = [], limit
    for target in targets:
        if not budget:
            files.append({
                "path": target.path, "source_area": target.area or "documents",
                "deferred": True, "truncated": True, "next_offset": offset,
            })
            continue
        if target.kind == "documents":
            item = documents.read_file(target.relative, offset=offset, limit=budget)
            item["truncated"] = not item["complete"]
        else:
            item = store.read_files(
                target.name, [target.relative], area=target.area, offset=offset, limit=budget,
            )["files"][0]
            item["complete"] = not item["truncated"]
        budget -= len(item["content"])
        item.update({"path": target.path, "source_area": target.area or "documents"})
        files.append(item)
    return {
        "action": "read", "files": files, "count": len(files), "offset": offset,
        "limit": limit, "offset_unit": "characters",
        "truncated": any(item["truncated"] for item in files),
        "complete": all(not item["truncated"] for item in files),
    }


def edit_workspace(action: str, path: str, args: dict) -> dict:
    target = address(path)
    require_file_scope(target.kind, write=True)
    if target.kind == "documents":
        if not target.relative or action not in {"create", "append", "edit"} or "files" in args:
            raise documents.InvalidArgumentsError("Documents support single-file create, append, and exact edit only")
        if action == "edit":
            payload = documents.edit_file(target.relative, args["old_text"], args["new_text"])
        else:
            operation = documents.create_file if action == "create" else documents.append_file
            payload = operation(target.relative, args["content"])
        return {**payload, "path": path, "source_area": "documents", "state_changed": True}
    if target.area != "draft":
        raise documents.InvalidPathError("Only draft source can be changed")
    require_development()
    if not target.relative:
        if action != "create" or "content" in args:
            raise documents.InvalidArgumentsError("Draft-root create accepts files; other operations require a file path")
        supplied = {}
        files = args.get("files", [])
        if not isinstance(files, list) or len(files) > store.MAX_PACKAGE_FILES:
            raise documents.InvalidArgumentsError(f"files must be an array of at most {store.MAX_PACKAGE_FILES} objects")
        seen = set()
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "content"}:
                raise documents.InvalidArgumentsError("Each file must contain only path and content")
            source = address(f"{path}/{entry['path']}") if isinstance(entry["path"], str) else None
            if source is None or not source.relative or not isinstance(entry["content"], str):
                raise documents.InvalidArgumentsError("Each file path and content must be strings")
            key = source.relative.casefold()
            if key in seen:
                raise documents.InvalidArgumentsError("Each file path must be unique")
            seen.add(key)
            supplied[source.relative] = entry["content"]
        payload = store.scaffold(target.name, files=supplied)
        payload["created_paths"] = [f"{path}/{item}" for item in payload["created_paths"]]
    else:
        if "files" in args:
            raise documents.InvalidArgumentsError("files is only accepted for draft-root create")
        operations = {"create": store.create_file, "append": store.append_file, "replace": store.write_file}
        if action in operations:
            payload = operations[action](target.name, target.relative, args["content"])
        elif action == "edit":
            payload = store.edit_file(target.name, target.relative, args["old_text"], args["new_text"])
        elif action == "remove":
            payload = store.remove_file(target.name, target.relative)
        else:
            raise documents.InvalidArgumentsError("Unsupported source operation")
    return {**payload, "action": action, "path": path, "source_area": "draft", "draft_path": target.draft_path}
