# Contributing to MochiBot

MochiBot is in early alpha. Contributions welcome!

1. Fork the repo
2. Create a feature branch
3. Follow the architecture in [ARCHITECTURE.md](ARCHITECTURE.md)
4. Submit a PR

---

## Adding a Custom Skill

Skills are active capabilities the Chat model can invoke via tool calls.

### Structure

```
mochi/skills/my_skill/
├── __init__.py          # empty
├── SKILL.md             # tool definitions (parsed at startup)
└── handler.py           # skill logic
```

### SKILL.md

```markdown
---
name: my_skill
expose: true
triggers: [tool_call]
---

## Tool: my_tool
Description: Does something cool

### Parameters
| Name | Type | Required | Description |
|------|------|----------|-------------|
| input | string | yes | The input to process |
```

### handler.py

```python
from mochi.skills.base import Skill, SkillContext, SkillResult

class MySkillHandler(Skill):
    async def execute(self, context: SkillContext) -> SkillResult:
        input_text = context.args.get("input", "")
        # Your logic here
        return SkillResult(output=f"Processed: {input_text}")
```

Restart MochiBot → check logs for `✅ Registered skill: my_skill`.

To disable: rename `SKILL.md` → `SKILL.md.disabled`.

---

## Adding a Custom Observer

Observers are passive, read-only sensors that feed context into the Heartbeat Think step — zero LLM calls.

There are two kinds:

| Type | Location | Toggle | Examples |
|------|----------|--------|----------|
| **Co-located** | `mochi/skills/{name}/` | Linked to skill toggle | oura, weather |
| **Infrastructure** | `mochi/observers/{name}/` | Always runs | time_context, activity_pattern |

### Co-located Observer (recommended for skill-specific data)

If your skill also needs to periodically collect data for the heartbeat:

1. Add `sense:` block to your SKILL.md:

```yaml
---
name: my_skill
expose_as_tool: true
sense:
  interval: 30
---
```

2. Create `observer.py` + `OBSERVATION.md` in the same skill directory:

```
mochi/skills/my_skill/
├── __init__.py
├── SKILL.md             # includes sense: block
├── handler.py           # skill logic
├── observer.py          # observer logic
└── OBSERVATION.md       # observer metadata
```

3. OBSERVATION.md:

```markdown
---
name: my_skill
interval: 30
type: source
enabled: true
requires_config: [MY_API_KEY]
skill_name: my_skill
---

Description of what this observer watches.
```

4. observer.py:

```python
from mochi.observers.base import Observer

class MyObserver(Observer):
    async def observe(self) -> dict:
        # Your logic here — return {} to report nothing
        return {"value": 42}
```

When the skill is disabled via admin, its observer is automatically skipped.

### Infrastructure Observer (for cross-cutting context)

For observers that are always needed and not tied to a specific skill:

```
mochi/observers/my_observer/
├── __init__.py
├── OBSERVATION.md
└── observer.py
```

OBSERVATION.md:

```markdown
---
name: my_observer
interval: 30
type: context
enabled: true
requires_config: []
---

Description of what this observer watches.
```

### Observer types

| Type | Meaning |
|------|---------|
| `source` | Fetches data from an external API or service |
| `context` | Derives context from internal state (DB, runtime) |

### Key behaviors

- **Auto-disabled** if any `requires_config` env var is missing at startup
- **Interval-throttled**: `safe_observe()` skips the call if last run < `interval` minutes ago
- **Error-isolated**: exceptions are caught and logged; returns `{}` on failure
- **5 consecutive failures** → auto-disabled for the session
- **Delta detection**: override `has_delta(prev, curr)` to suppress noisy Think triggers
- Return `{}` to silently skip a tick (e.g. API unavailable)

Restart MochiBot → check logs for `✅ Registered observer: my_observer`.

To disable: set `enabled: false` in `OBSERVATION.md`, or rename it `OBSERVATION.md.disabled`.

---

## Code Style

- **Comments**: English only
- **Commit messages**: English, [conventional commits](https://www.conventionalcommits.org/)
- **Naming**: English, camelCase for variables/functions
- See [ARCHITECTURE.md](ARCHITECTURE.md) for layer rules and dependency direction
