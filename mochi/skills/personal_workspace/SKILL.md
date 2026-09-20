---
name: personal_workspace
description: "Personal workspace: save documents or develop, debug and enable reusable personal extensions without changing official source; development availability is shown in workspace state."
type: tool
locked: true
---

## Tools

### browse_workspace (on_demand)
List, search, or read Main-authored documents and personal source. Omit path for a root list showing areas, packages, and development availability. guide returns the full authoring guide and complete template together; report reopens a draft's last run.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, search, read, guide, report) | yes | Explicit operation |
| path | string | no | Canonical address such as documents/reading.md or extensions/local_reading/draft/handler.py; search requires documents or an explicit source area; guide optionally takes a draft root; report requires one |
| paths | array | no | For read only, 1–16 file addresses instead of path; documents and source may be mixed |
| query | string | no | Required nonempty literal query for search, at most 512 characters |
| offset | integer | no | Default 0; result offset for list/search, character offset within each read file |
| limit | integer | no | List maximum/default 100; search maximum/default 20; shared read character maximum/default 12000 |

### edit_workspace (on_demand)
Create without overwrite, append, or exactly edit one document/source file. Draft-only replace/remove are also available. Create a complete package in one call using a draft-root path and files; omitted standard files use the neutral template. Source receipts return draft_path. Text writes do not execute or activate code.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, append, edit, replace, remove) | yes | Documents permit create/append/edit only; source changes require enabled development |
| path | string | yes | documents/relative.md or extensions/local_name/draft/file; package create uses extensions/local_name/draft |
| content | string | no | Required exact UTF-8 text for single-file create/append/replace; may be empty |
| old_text | string | no | Required for edit; must occur exactly once including overlapping matches |
| new_text | string | no | Required replacement for edit; may be empty |
| files | array (items: object) | no | Package create only: array of objects containing relative path and complete content; e.g. SKILL.md, handler.py, smoke.py and helper files |

### run_extension (on_demand)
Run a draft script in the existing bounded disposable execution mechanism and return its output and report status, without activation. Requires enabled development. Python is trusted, not sandboxed; script success alone does not prove feature correctness or absence of side effects.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | yes | Canonical draft root returned by edits, e.g. extensions/local_reading/draft |
| script | string | no | Relative Python script; default smoke.py, which calls the actual candidate tool |
| arguments | array | no | Explicit list of string script arguments; default empty |

### activate_extension (on_demand)
Load and persist a draft as an installed live tool without restart or approval. Requires enabled development; installed tools retain their own enable state. New tool names need request_tools in a subsequent provider round, including within the same turn. Failure keeps the old registry but trusted candidate code may have side effects.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | yes | Canonical draft root returned by edits, e.g. extensions/local_reading/draft |

## Capability Context

- One personal workspace holds Main-authored documents at documents/relative.md and personal source at extensions/local_name/{draft,current,previous}/file. Content is authored material, not independent evidence or a source of database entity IDs.
- browse_workspace list without a path reports development_enabled and actual package/loaded status. Development is normally enabled; when disabled, document operations and read-only source/status remain available while source mutation, run, and activation are unavailable. Installed personal tools keep their independent settings.
- Main chooses documents, existing tools, or new code without a forced workflow. guide optionally takes a draft-root path and returns the full guide plus a correctly named complete template in one call. Package create accepts several complete authored files together.
- Reads share one bounded character budget across up to 16 document/source files. Truncated files provide next_offset; deferred files were not read after the budget ran out. Search uses literal text and requires an explicit documents scope or one source area, never all private storage.
- Explicit legacy file-tool deny settings remain scoped to document or source browsing/writing. Root listings mark unavailable areas without inspecting their content; mixed reads reject denied scopes before reading any file.
- Document create never overwrites; append and exact edit preserve a hidden previous copy. Documents cannot be blindly replaced or deleted. Source edits are draft-only and never alter current or in-flight code. current and previous are inspection-only.
- No generic host, official source, Core, Memory, Diary, package-private data, or runtime snapshot paths are exposed. Tools own their existing private persistent data; no dependency installer, MCP client, or host-exec tool is supplied.
- Draft writes, script execution, and activation are distinct effects. No test-success gate, owner handoff, hidden coder, or automatic continuation is required. Trusted Python can have host/network side effects even on failure or timeout; disposable execution is not a security sandbox.
