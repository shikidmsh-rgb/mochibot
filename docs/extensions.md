# Personal extensions

Personal extensions add ordinary tools without editing official Mochi source.
They are trusted Python code, not a sandbox. Development is enabled by default
and appears in Main's capability summary without separate authorization.
Main chooses what to build, how to test, and when to activate it, using four
on-demand tools in conversation or eligible autonomous entries. Admin is an
optional control and status view; the ordinary skill toggle can disable
development, and an explicit disabled setting is preserved across restarts.
No Admin visit or restart is required:

- `inspect_extension`: the guide, template, package state, source, and run report.
- `write_extension`: create, write, edit, or remove draft source.
- `run_extension`: execute a draft script and return real diagnostics.
- `activate_extension`: load and persist a candidate, then replace live tools.

These are capabilities, not a prescribed workflow. There is no hidden coder,
mandatory verification-success gate, automatic retry, or continuation loop.

## Package contract

A personal ID looks like `local_reading`. Its tool names begin with that ID
and an underscore, such as `local_reading_add`. Names must not collide with
another skill or framework tool.

Each draft contains `__init__.py`, `SKILL.md`, `handler.py`, and usually
`smoke.py`. `inspect_extension(action="guide")` returns this guide and a complete
runnable template. `write_extension(action="create")` can receive your complete
`skill_md`, `handler_py`, and `smoke_py` strings together; omitted files use the
neutral template, not an implementation inferred from your request.

`SKILL.md` defines the name, description, `type: tool`, configuration, and
ordinary tool schemas using the existing Markdown parameter tables. Tools use
`resident`, `routed`, or `on_demand`. Capability Context describes the capability's
effects and real constraints, not a compulsory sequence for Main.

`handler.py` defines one local subclass of
`mochi.skills.base.Skill` and implements `async execute(context)`.
Use `SkillContext.args`, `SkillResult`, relative imports of your own helpers,
`self.config`, and the injected `self.data_dir`. Store persistent files or a
private SQLite database under `self.data_dir`; it survives code replacement.
The handler returns `SkillResult(success=False, output=...)` for known failures
and reports actual state changes with `state_changed=True`.
Regular constructors also receive the injected configuration and data directory;
custom object allocation (`__new__`) is not supported.

Do not use the shared application database or import configuration/storage
helpers that load live application state. Schema initialization, Observer,
Diary/prompt/lifecycle hooks and custom dispatch methods are outside this
version's extension contract. There is no automatic dependency installation:
use Python's standard library or already installed libraries.

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

The read and write tools have separate allowances. Inspect several files with
one `paths` array; create a small complete package in one write call. Later edits
can replace one exact occurrence in a draft file.

`run_extension` defaults to `smoke.py`. The template's smoke script loads the
actual candidate with temporary extension data and calls its real `Skill.run()`.
It checks `SkillResult.success`, not merely whether Python starts. Modify its
arguments and expectations along with your handler. Main authors these
expectations; the harness does not decide whether your feature meets the goal.

Execution uses a disposable copy and bounded time/output. Returned diagnostics
are the actual process output. The last run report can be reopened. An arbitrary
script can still access other files or services: temporary inputs and a
subprocess are not containment. Failure or timeout does not prove zero effects,
and no failed command is automatically replayed.

Drafts persist across turns. Existing tool budgets still apply, and no hidden
loop resumes development after the turn ends.

## Activation and use

`activate_extension` copies the draft into an immutable runtime snapshot, loads
the candidate with its configured values and persistent data directory, and
validates registration and tool ownership. Only then does it persist the
installed `current` version, retain one `previous` version, and swap the live
registry. A disabled extension must be enabled separately. Structural and load
checks do not judge whether Main's implementation meets its goal.

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

Official updates do not replace personal code or data. They can still change
interfaces or dependencies, so a future update may require repairing an
extension. This feature does not add MCP, OpenClaw skill compatibility, automatic
file-watching reload, background self-learning, or restart coordination.
