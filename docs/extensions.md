# Personal extensions and workspace

`personal_workspace` is Main's single entry for persistent documents and personal
tool source. **Personal extensions** are reusable tools authored independently
of official source, by Main or the owner. Users can describe what they need
without choosing a package type or knowing extension terminology.
The workspace does not replace Diary, Core, or memory. Main chooses what to
write and whether executable behavior is useful; a document does not need to
become a program. Four on-demand tools are available in conversation and
eligible autonomous entries:

- `browse_workspace`: list, search, or read documents/source; inspect the guide,
  complete template, package state, or last run report.
- `edit_workspace`: create, append, or exact-edit files; replace/remove draft
  source, or create a complete authored package in one call.
- `run_extension`: execute a draft script and return real diagnostics.
- `activate_extension`: load and persist a candidate, then replace live tools.

These are capabilities, not a prescribed workflow. There is no hidden coder,
mandatory verification-success gate, automatic retry, or continuation loop.
Saving source does not run it; running source does not activate it.

## Workspace files

Use workspace-relative paths, not host paths:

```text
documents/health/record.md
extensions/local_example/draft/handler.py
extensions/local_example/current/handler.py
extensions/local_example/previous/handler.py
```

Document paths map to the existing `data/mochi_files/` Markdown space.
Extension paths map to existing `data/extensions/` packages. Existing files,
previous versions, configuration and tool data stay in place; no migration is
required. `browse_workspace(action="list")` shows the two areas and current
development state. Multi-file reads share a bounded response. Literal search
can target documents or an explicitly addressed source area.

Documents support create-without-overwrite, append and exact edits, retaining
the existing hidden previous copy. They do not support blind replacement or
deletion. Source mutation affects only drafts; `current` and `previous` are
read-only. Package-owned data, runtime copies, credentials, official source and
the shared database are not generic workspace file paths.

Development is enabled by default, without separate authorization. The existing
`toggle_skill(skill_name="development", enabled=false)` setting disables draft
mutation, execution and activation, not document operations, read-only source
inspection, or already installed personal tools. Admin offers the same optional
setting; explicit disables persist. `development` and `mochi_files` are no longer
separate skill entries. Exact legacy discovery requests point to the new entry,
but retired tool names are not executable.

Personal extensions are trusted local Python, not a sandbox. No Admin visit or
restart is required to author and activate them.

## Personal extension API v1

The public interface is `mochi.mod_api.v1`. It supports ordinary tool
extensions, not Observer sources, MCP connections or lifecycle hooks.

A personal ID looks like `local_reading`. Its tool names begin with that ID
and an underscore, such as `local_reading_add`. Names must not collide with
another skill or framework tool.

Each draft contains `__init__.py`, `SKILL.md`, `handler.py`, and usually
`smoke.py`. `browse_workspace(action="guide",
path="extensions/local_reading/draft")` returns this guide and a complete
runnable template for that path. `edit_workspace(action="create",
path="extensions/local_reading/draft", files=[...])` accepts file entries with
`path` and `content`, such as `SKILL.md`, `handler.py`, and `smoke.py`. Omitted
standard files use the neutral template, not an inferred implementation.
Source receipts include `draft_path`; use that same path for run and activation.

`SKILL.md` is the single manifest. Its front matter defines the name,
`mod_api: 1`, description, `type: tool`, configuration, and
ordinary tool schemas using the existing Markdown parameter tables. Tools use
`resident`, `routed`, or `on_demand`; parameters support scalars and string arrays.
Capability Context describes the capability's effects and real constraints, not
a compulsory sequence for Main.

```yaml
---
name: local_reading
mod_api: 1
description: Personal reading tools.
type: tool
---
```

Declare `mod_api` once as a positive decimal integer. Missing `mod_api` means
**legacy v1**, supported without rewriting source, migrating data or requiring
reactivation. Explicit `1` selects v1. Other positive integer versions fail with
`unsupported_mod_api`, reporting the required and supported versions; malformed
or duplicate declarations fail with `invalid_mod_api`. Do not change the version
label alone to "repair" incompatible code.

### Public Python API

New `handler.py` files define one local subclass of `Skill` and implement
`async execute(context)` through this import surface:

```python
from mochi.mod_api.v1 import Skill, SkillContext, SkillResult


class PersonalSkill(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        return SkillResult(output=str(context.args["text"]))
```

These are the same classes as the supported legacy
`from mochi.skills.base import Skill, SkillContext, SkillResult` imports, not a
second runtime. Importing the public API does not initialize application
configuration, the database or runtime services.
Use `SkillContext.args`, normal `SkillResult` constructors, relative imports of
your own helpers, `self.config`, and the injected `self.data_dir`. Store persistent files or a
private SQLite database under `self.data_dir`; it survives code replacement.
Regular constructors also receive the injected configuration and data directory;
custom object allocation (`__new__`) is not supported.

The existing context/result constructor fields and defaults are part of v1.
Additional optional fields may be added while preserving existing constructor
behavior. Mutable defaults are fresh per instance:

| `SkillContext` field | Default / meaning |
|---|---|
| `trigger` | Required invocation kind; ordinary live tools receive `"tool_call"`; smoke scripts may use `"script"`. |
| `user_id`, `channel_id` | `0`; caller identifiers. |
| `transport`, `actor` | `""`; supplied caller transport and actor. |
| `owner_authorized` | `False`; supplied owner authorization, not a handler claim. |
| `tool_name` | `""`; tool being called. |
| `args` | `{}`; invocation arguments. |
| `observation` | `None`; retained constructor field, not an Observer API for personal extensions. |

| `SkillResult` field | Default / meaning |
|---|---|
| `output` | `""`; result text or a useful failure diagnostic. |
| `success` | `True`; use `False` for a known failure. |
| `summary`, `entity_refs` | `""`, `[]`; deterministic receipt text and stable references for actual results. |
| `state_changed` | `False`; report known durable changes truthfully. |
| `error_code`, `retryable` | `""`, `None`; optional machine-readable failure and retry facts. |
| `content_source` | `""`; provenance of returned content, not an authority grant. |
| `actions` | `[]`; retained result payload, not permission to add Observer or delivery hooks. |
| `execution_started`, `state_change_unknown` | `False`, `False`; execution/uncertainty facts described below. |

Handlers author outputs, receipts and known outcome facts; the runtime owns
dispatch, execution evidence and audit-record creation. `Skill.run()` sets
`execution_started=True` after entering the handler, including when it returns
a known failure. An unhandled exception becomes a failed result with
`error_code="skill_exception"`, `retryable=False`, and
`state_change_unknown=True`; that uncertainty is not a rollback claim.
A rejection before entering the handler has no handler execution evidence.
Do not manufacture evidence by setting `execution_started`, inventing receipt
references or returning success prose. Handler-authored fields describe the
outcome; they do not create independent proof of execution or external effects.

Do not use the shared application database or import configuration/storage
helpers that load live application state. Schema initialization, Observer,
Diary/prompt/lifecycle hooks and custom dispatch methods are outside this
version's extension contract. Registry internals and arbitrary private Base
imports are not public personal extension APIs. This boundary is not a Python sandbox:
third-party imports remain allowed, using installed libraries. There is no
automatic dependency installer, dependency version solver or per-extension environment.

Configuration uses existing skill metadata; `get_skill_config` and
`set_skill_config` manage it in conversation, with an optional Admin card:

```yaml
config:
  ACCESS_TOKEN:
    type: str
    default: ""
    secret: true
    description: "Token for the service"
requires_config: [ACCESS_TOKEN]
```

Declare every required key in `config` so it can be supplied and injected.
Configuration management includes newly declared draft keys while the installed
tool keeps its current schemas and availability until activation.
Never put credentials in source. Missing configuration makes the live tool
unavailable until the owner supplies it. Smoke scripts use explicit sample
configuration instead of loading production credentials.

## Authoring and trying the tool

Inspect several files with one `paths` array; create a small complete package
in one write call. Later edits can replace one exact occurrence in a draft file.

`run_extension(path="extensions/local_reading/draft")` defaults to `smoke.py`.
The template's smoke script uses the public helper:

```python
from mochi.mod_api.v1 import SkillContext, run_candidate

skill = run_candidate(extension_id, package_dir, data_dir, config={"KEY": "sample"})
```

`package_dir` and `data_dir` are `Path` objects. The helper returns the loaded
`Skill`, using typed schema defaults plus the optional explicit configuration
dictionary, never live database/environment credentials. It neither activates
nor registers a tool. The template loads the actual candidate with disposable
extension data supplied by the runner and calls its real `Skill.run()`.
It checks `SkillResult.success`, not merely whether Python starts. Modify its
arguments and expectations along with your handler. Main authors these
expectations; the harness does not decide whether your feature meets the goal.

Execution uses a disposable copy and bounded time/output. Returned diagnostics
are the actual process output. The last run report can be reopened. An arbitrary
script can still access other files or services: temporary inputs and a
subprocess are not containment. Failure or timeout does not prove zero effects,
and no failed command is automatically replayed.

Drafts persist across turns. Workspace tools share the ordinary-call budget
(24 by default) without positive per-tool caps. The provider-round budget
(16 by default) and script time/output bounds still apply; explicit lower total
budgets remain effective. No hidden loop resumes development after the turn ends.
The candidate helper enforces API compatibility before importing the package.
Directly running an explicitly selected draft script remains trusted Python
execution, not a blanket API-version gate or proof the package can be activated.

## Activation and use

`activate_extension(path="extensions/local_reading/draft")` loads an immutable
candidate with its configuration and persistent data, validates tool ownership,
then persists `current`, retains one `previous` version and swaps the registry.
A disabled extension must be enabled separately. Structural checks do not judge
whether the implementation meets Main's goal.

Failed activation preserves the old registry, but candidate imports or
constructors may already have affected data or external services. In-flight
calls keep their old code; later provider rounds refresh already authorized
tools. New names require `request_tools` and become usable in a subsequent
provider round, without restarting or starting another conversation.

Startup loads enabled installed packages. Ordinary rediscovery does not import
newly restored code; explicit activation or enable/load does. Management reads
metadata without importing unloaded code, so broken packages remain inspectable
and disableable. Enabling an installed package attempts a live load; a draft
still needs activation. Load failures report both the error and saved enable flag.

Receipts distinguish `loaded`, `activation_required`, `load_error`,
`admin_disabled` and missing configuration. Their `mod_api` object describes
existing `draft` and `current` areas plus the loaded `active` snapshot (or null).
Each reports `declared`, `effective` and `status`: legacy, supported, unsupported,
invalid, or unavailable with a read error. Legacy v1 has declared null/effective 1;
explicit v1 has declared 1/effective 1. An incompatible draft does not relabel the
working active version. The guide reports `supported_mod_apis: [1]`.

Activation, startup, explicit loading and the candidate helper reject invalid
or unsupported API declarations before importing package code. Other valid
packages remain usable. Main chooses whether to repair or abandon an extension.

## Recovery and limits

Disabling Development leaves installed tools enabled. A broken package can be
disabled through conversation management or Admin without loading its code.
Main can repair its draft while the previous live version keeps running.

If extension code prevents startup, stop Mochi and rename its directory to
start with an underscore, such as `_disabled_local_reading`; startup ignores it.
While stopped, the owner may restore `previous` and restart. This restores code
only, not data or external effects, and older code may not understand newer data.
There is no automatic rollback.

### Compatibility across Base updates

Base preserves the v1 public API, documented manifest behavior, configuration,
enable flags, tool names and extension-owned data across updates, including
legacy imports and versionless v1 packages. A compatibility break is a Base
regression, not a repair task for Main. Updates do not rewrite personal source
or require migration.

Private Base imports, third-party dependencies, external services and an
extension's own data-schema changes are outside that promise. The repository's
Python baseline applies (currently 3.11+); templates need no extra dependencies.
There is no automatic dependency installation, file-watching reload or
background self-learning.
