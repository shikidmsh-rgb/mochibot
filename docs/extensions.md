# Personal workspace and extensions

`personal_workspace` is Main's single entry for persistent documents and personal
tool source. It does not replace Diary, Core, or memory. Main chooses what to
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

## Tool Mod contract v1

A personal executable extension is a **Mod**. This contract currently covers
ordinary tools only; it does not add Observer sources, MCP connections or
lifecycle hooks. Existing extension paths, `local_*` IDs, Skill names and tool
names are unchanged.

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
| `observation` | `None`; retained constructor field, not an Observer API for Mods. |

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
imports are not public Mod APIs. This boundary is not a Python sandbox:
third-party imports remain allowed, using installed libraries. There is no
automatic dependency installer, dependency version solver or per-Mod environment.

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

The unified read and edit tools each pool the former two tools' per-turn
allowances (six calls each at the default), still within eight total ordinary
calls. Other tools keep their per-name allowance. Inspect several files with
one `paths` array; create a small complete package in one write call. Later edits
can replace one exact occurrence in a draft file.

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

`activate_extension(path="extensions/local_reading/draft")` copies the draft
into an immutable runtime snapshot, loads
the candidate with its configured values and persistent data directory, and
validates registration and tool ownership. Only then does it persist the
installed `current` version, retain one `previous` version, and swap the live
registry. A disabled extension must be enabled separately. Structural and load
checks do not judge whether Main's implementation meets its goal.
Activation, startup loading, explicit live enabling and the candidate helper
all reject an unsupported or invalid API declaration before importing either
`__init__.py` or `handler.py`.

Failure leaves the previous registry intact, but imports or constructors may
already have changed extension data or external services. There is no rollback
of those effects. Calls already running finish on their old immutable code;
later provider rounds refresh tools already authorized for the turn.
New tool names still need `request_tools` and cannot be used in the same provider
response that requests them. Main can activate and use a new tool in subsequent
rounds of the same conversation turn, without a restart.

Files live in the Git-ignored `data\extensions\<id>\` directory:
`draft`, `current`, `previous`, and persistent `data`, plus temporary loaded
copies. Draft edits never change running code. Startup loads enabled installed
current versions. Ordinary rediscovery does not import newly restored code;
explicit activation or enable/load can do that live.

Management reads metadata without importing unloaded Python, including broken
or disabled packages. Its `loaded`, `activation_required`, `load_error`,
`admin_disabled`, and configuration facts distinguish saved settings from real
availability. Enabling an installed package attempts a live load; a draft alone
still needs activation. A failed load reports the error and the saved enable
flag rather than claiming success.

Personal package receipts include a `mod_api` object with separately labeled
`draft` and `current` entries for areas that exist, plus `active` metadata when
loaded (`null` otherwise). Each entry reports `declared`, `effective`, and
`status`: `legacy`, `supported`, `unsupported`, or `invalid`, with diagnostics
when needed. Management reports unreadable metadata as `unavailable` with an
error, rather than claiming an unsupported version. Legacy v1 has
`declared: null`, `effective: 1`; explicit v1 has
`declared: 1`, `effective: 1`. `active` describes the loaded snapshot, not editable
disk metadata. An incompatible draft does not make a working current tool
unsupported. API support remains separate from `loaded`, `load_error`,
`activation_required`, missing configuration and semantic usefulness.
The on-demand guide reports `supported_mod_apis: [1]`.

An unsupported installed package stays inspectable and disableable while other
valid packages load. Rejected activation leaves the previous registration
intact; Main chooses whether to repair the code, retain it, seek a compatible
Base or abandon the Mod. The runtime does not silently relabel it, fall back to
previous code or automate a repair workflow.

## Recovery and limits

Loading errors are visible in conversation management and Admin; an extension
can be disabled even if it could not register. Disabling Development does not
disable already installed personal tools. Main can repair a draft and activate
the repaired version while the previous live version keeps running.

If extension code prevents startup, stop Mochi and rename its entire directory
to start with an underscore, for example `_disabled_local_reading`. Such
directories are ignored before executing any extension code.
While stopped, you may restore the previous code copy and restart. Restoring
code never rolls back data or external actions, and does not guarantee older
code understands newer data. There is no automatic rollback.

### Compatibility across Base updates

Base maintains the v1 public API, documented `SKILL.md` semantics,
configuration/data identity and supported loading/execution behavior, including
legacy imports and versionless v1 packages. Internal refactors must preserve
that behavior or adapt behind the public API, even if a later API is added.
A supported-v1 compatibility break is a Base regression to fix, not an expected
repair task for Main. Updates must not rewrite personal source, reset saved
configuration or enable flags, move/recreate package data, rename public tools
or silently discard v1. No migration is required.

This is a host-contract promise, not a guarantee for arbitrary Base-private
imports, third-party libraries, remote providers or a Mod's own data-schema
changes. The Python baseline remains the repository's supported baseline
(currently Python 3.11+). Base dependency/runtime changes must be checked against
the v1 commitment, but not every installed library or external service can be
promised unchanged. Existing third-party imports remain usable; new templates
need no extra dependency installation.

This feature does not add MCP, OpenClaw skill compatibility, automatic
file-watching reload, background self-learning or restart coordination.
