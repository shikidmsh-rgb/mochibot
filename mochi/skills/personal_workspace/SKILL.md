---
name: personal_workspace
description: "Save and revise personal documents, create reusable document skills, or build Python tools."
type: tool
locked: true
---

## Tools

### browse_workspace (on_demand)
Browse your saved documents and personal skills. Omit path to see the workspace and available operations. Use list, search or read for files; guide returns the authoring guide with document and Python templates; report reads a draft's last script-run report.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: list, search, read, guide, report) | yes | Explicit operation |
| path | string | no | Canonical address such as documents/reading.md or extensions/local_reading/draft/handler.py; search requires documents or an explicit source area; guide optionally takes a draft root; report requires one |
| paths | array | no | For read only, 1–16 file addresses instead of path; documents and source may be mixed |
| query | string | no | Required nonempty literal query for search, at most 512 characters |
| offset | integer | no | Default 0; result offset for list/search, character offset within each read file |
| limit | integer | no | List maximum/default 100; search maximum/default 20; shared read character maximum/default 12000 |

### edit_workspace (on_demand)
Create or revise personal documents and skill drafts. A draft-root create accepts files; kind: document in SKILL.md makes a document skill without Python. Other new packages fill omitted standard files from the Python template. Editing a draft does not change the active skill.

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
Activate a skill draft and keep the previous version. Document skills load text; Python extensions run trusted local code. You can request newly added tools with request_tools and then use them in this conversation. Failed activation leaves the currently loaded tools unchanged; Python code may already have caused effects.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | yes | Canonical draft root returned by edits, e.g. extensions/local_reading/draft |

## Capability Context

- One personal workspace holds Main-authored documents at documents/relative.md and personal source at extensions/local_name/{draft,current,previous}/file. Content is authored material, not independent evidence or a source of database entity IDs.
- Document create never overwrites; append and exact edit preserve a hidden previous copy. Documents cannot be blindly replaced or deleted. Source edits are draft-only and never alter current or in-flight code. current and previous are inspection-only.
- No generic host, official source, Core, Memory, Diary, package-private data, or runtime snapshot paths are exposed. Tools own their existing private persistent data; no dependency installer, MCP client, or host-exec tool is supplied.
