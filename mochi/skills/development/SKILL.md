---
name: development
description: "Develop personal tools: inspect the extension guide and templates, author Python skills, run and debug them, and activate them live."
type: tool
---

## Tools

### inspect_extension (on_demand)
Read the development guide and complete template together, list personal packages and their actual loaded states, read several code files in one call, or inspect the last run.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: guide, list, read, report) | yes | guide includes a ready-to-run template; list includes load and activation facts |
| extension_id | string | no | Personal ID such as local_reading; required for read/report, optional for guide/list |
| paths | array | no | Relative code file paths for read; multiple files share one read allowance |
| area | string (enum: draft, current, previous) | no | Code area to read; default draft |
| offset | integer | no | Character offset within each requested file; default 0 |
| limit | integer | no | Total bounded read character budget; default 12000 |

### write_extension (on_demand)
Create a complete draft with your own metadata, handler and smoke script in one call, or change a draft file. Read and write allowances are separate. Missing create content uses the neutral template, not an inferred implementation.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| action | string (enum: create, write, edit, remove) | yes | create scaffolds a new draft; other actions affect one draft file |
| extension_id | string | yes | Personal ID beginning with local_ |
| skill_md | string | no | Complete Main-authored SKILL.md for create |
| handler_py | string | no | Complete Main-authored handler.py for create |
| smoke_py | string | no | Complete Main-authored smoke.py for create |
| path | string | no | Relative draft file path for write/edit/remove |
| content | string | no | Exact UTF-8 text for write |
| old_text | string | no | Nonempty text occurring exactly once for edit |
| new_text | string | no | Replacement text for edit; may be empty |

### run_extension (on_demand)
Run a Python script from a disposable copy of a draft and return its real output and exit status. The default smoke.py calls the actual candidate tool. A successful script alone is not proof of the feature's correctness.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| extension_id | string | yes | Personal draft to run |
| script | string | no | Relative Python script; default smoke.py |
| arguments | array | no | Additional string arguments; normally unnecessary for smoke.py |

### activate_extension (on_demand)
Load a complete draft and publish it as the live, installed version without restarting. Existing calls finish on their old code snapshot. New tool names still need request_tools for a subsequent provider round, which can be in this same turn. A disabled extension must be enabled separately.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| extension_id | string | yes | Personal draft to activate |

## Capability Context

- Development is enabled by default and available to Main on demand, including eligible autonomous entries. No separate authorization is needed. The ordinary skill toggle can disable it; Admin is an optional control surface, not a required handoff.
- inspect_extension guide returns the current authoring contract and runnable template together. create accepts your complete source for several files in one call; all business logic and smoke expectations are yours.
- Main chooses what to build, how to test, and when to activate; there is no mandatory workflow, test-success gate, hidden coder, or automatic continuation loop. Drafts and the last run report persist across turns; existing call limits still apply.
- File operations affect personal drafts, not official source, installed code, or extension data. Activation loads an immutable candidate with its configured data directory, checks ownership, retains one previous installed version, and swaps the live registry only on success. Failure preserves the old registry but may leave effects from trusted candidate code.
- Later provider rounds refresh already-authorized tools. Newly added names require request_tools as usual and cannot be used in the provider response that requests them. inspect_extension list reports the actual loaded state; draft edits never change running code.
- Ordinary Python tools can use installed libraries and their own data directory. Observer, shared database, prompt/lifecycle hooks, dependency installation and MCP integration are not provided.
- Python is trusted local code, not a sandbox. Temporary inputs do not prevent explicit access to other files or services. Failures and timeouts do not prove that no side effects occurred.
